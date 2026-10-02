"""Task resolution orchestration over CFW, provider, guard, and policy ports."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Union

from runtime_contract.advisor import (
    BehavioralAdvisor,
)
from runtime_contract.actions import action_contract_hash, effective_action_registry
from runtime_contract.handlers import enforcement_for_guard, handler_registry_hash
from runtime_contract.routing import (
    applicable_adjustments, assign_experiment_cohort, comparison_context_hash,
    load_routing_profiles, validate_experiment_plan,
)
from runtime_contract.statework_provider import DEFAULT_STATEWORK_ROOT
from runtime_contract.task import SystemOnePolicy, TaskRequest
from runtime_contract.validators import validator_registry_hash
from runtime_contract.workflow_port import (
    WorkflowPlanner,
)
from resolver.build_manifest import runtime_behavior_hash, runtime_build_manifest
from resolver.common import _load_json, _stable_json, load_yaml, sha256, sha256_bytes
from resolver.components import expand, load_pipeline
from resolver.guards import (
    load_guard_pack, semantic_guard_hash,
)
from resolver.policy_snapshot import (
    CapsulePin, EffectivePolicy, GuardPin, PolicySnapshot, RuntimeBundle,
    SnapshotIntegrityError, StateWorkPin, _policy_from_bundle, compute_policy_hash,
    injected_capsules_for,
)
from resolver.task_workflows import (
    resolve_prepared_task_workflows, resolve_task_workflows,
)
from resolver.task_guards import resolve_task_guards
from resolver.task_advice import resolve_task_advice
from resolver.task_kernel import compose_task_kernel

ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONTRACT_ROOT = ROOT / "contracts"
if not LOCAL_CONTRACT_ROOT.exists():
    LOCAL_CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "contracts"


def _load_document(path: Path, loader: Callable[[Path], Any]) -> Any:
    if not path.exists():
        raise SnapshotIntegrityError(f"required policy contract is missing: {path}")
    return loader(path)


def resolve(task_id: Union[str, TaskRequest], shape: str = "implement", domains: Optional[Iterable[str]] = None,
            model: Optional[str] = None, harness: Optional[str] = None,
            toolset: Optional[str] = None, trigger: Optional[str] = None,
            packet_store: Optional[Dict[str, Dict[str, Any]]] = None,
            cow_root: Path = DEFAULT_STATEWORK_ROOT,
            allow_missing_required: bool = False,
            missing_required: Optional[List[str]] = None,
            requested_flows: Optional[Dict[str, str]] = None,
            subject_ref: Optional[str] = None,
            phase: Optional[str] = None,
            operation: Optional[str] = None,
            observation_context: Optional[Mapping[str, Any]] = None,
            goal_contract: Any = None,
            agent_id: str = "agent",
            application_id: str = "cognitiveframeworks-runtime",
            application_version: Optional[str] = None,
            application_instance_id: Optional[str] = None,
            provider_id: Optional[str] = None,
            model_id: Optional[str] = None,
            model_revision: Optional[str] = None,
            model_capability_hash: Optional[str] = None,
            harness_version: Optional[str] = None,
            namespace_id: str = "default",
            action_registry: Optional[Mapping[str, Any]] = None,
            routing_profile_path: Optional[Path] = None,
            behavioral_advisor: Optional[BehavioralAdvisor] = None,
            sharing_verifier: Optional[Callable[[Mapping[str, Any], str, str], bool]] = None,
            system_one_policy: Optional[Union[SystemOnePolicy, Mapping[str, Any]]] = None,
            workflow_port: Optional[WorkflowPlanner] = None) -> RuntimeBundle:
    """Pure library entry point: TaskRequest -> RuntimeBundle. No source-tree
    writes (the CLI snapshot writer is separate and out-of-tree)."""
    if isinstance(task_id, TaskRequest):
        request = task_id
        task_id = request.task_id
        application_id = request.application_id
        application_version = request.application_version
        application_instance_id = request.application_instance_id
        namespace_id = request.namespace_id
        shape = request.shape
        domains = list(request.domain_tags)
        subject_ref = request.subject_ref
        agent_id = request.agent_id
        agent_instance_id = request.agent_instance_id or request.agent_id
        attempt_id = request.attempt_id
        interaction_id = request.interaction_id
        parent_session_id = request.parent_session_id
        delegator_agent_id = request.delegator_agent_id
        delegate_agent_id = request.delegate_agent_id
        delegation_id = request.delegation_id
        role = request.role
        parent_decision_ref = request.parent_decision_ref
        shared_evidence_refs = list(request.shared_evidence_refs)
        delegation_topology = request.delegation_topology
        premise_digest = request.premise_digest
        failure_attribution = request.failure_attribution
        adopted_profile_hashes = tuple(request.adopted_profile_hashes)
        shared_workflow_membership = (dict(request.shared_workflow_membership)
                                      if request.shared_workflow_membership is not None else None)
        shared_workflow_memberships = tuple(dict(item)
                                            for item in request.shared_workflow_memberships)
        statework_subject_refs = dict(request.statework_subject_refs)
        policy_revocation_revision = request.policy_revocation_revision
        revocation_mode = request.revocation_mode
        routing_experiment_plan = dict(request.routing_experiment_plan)
        phase = request.phase
        operation = request.operation
        trigger = request.trigger
        model = request.model
        model_id = request.model_id
        provider_id = request.provider_id
        model_revision = request.model_revision
        model_capability_hash = request.model_capability_hash
        harness = request.harness
        harness_version = request.harness_version
        toolset = request.toolset
        observation_context = dict(request.observation_context)
        goal_contract = request.goal_contract
        system_one_policy = request.system_one_policy
        if action_registry is None:
            action_registry = request.action_registry
    else:
        domains = list(domains or [])
        phase = None
        agent_instance_id = agent_id
        attempt_id = "attempt-1"
        interaction_id = None
        parent_session_id = None
        delegator_agent_id = None
        delegate_agent_id = None
        delegation_id = None
        role = None
        parent_decision_ref = None
        shared_evidence_refs = []
        delegation_topology = None
        premise_digest = None
        failure_attribution = "none"
        adopted_profile_hashes = ()
        shared_workflow_membership = None
        shared_workflow_memberships = ()
        statework_subject_refs = {}
        policy_revocation_revision = 0
        revocation_mode = "deferred"
        routing_experiment_plan = {}
        goal_contract = None
        system_one_policy = system_one_policy or SystemOnePolicy()
        application_instance_id = application_instance_id or application_id
        model_id = model_id or model
    application_id = str(application_id or "")
    namespace_id = str(namespace_id or "")
    application_instance_id = str(application_instance_id or "")
    if not application_id or not namespace_id or not application_instance_id:
        raise ValueError("application_id, namespace_id, and application_instance_id are required")
    if model is not None and model_id is not None and model != model_id:
        raise ValueError("model and model_id must agree when both are provided")
    model_id = model_id or model
    model = model_id
    task_id = str(task_id)
    agent_id = str(agent_id or "agent")
    goal_contract_dict = goal_contract.to_dict() if goal_contract is not None else {}
    goal_contract_hash = goal_contract.contract_hash if goal_contract is not None else ""
    if not isinstance(system_one_policy, SystemOnePolicy):
        if not isinstance(system_one_policy, Mapping):
            raise ValueError("system_one_policy must be a SystemOnePolicy or mapping")
        system_one_policy = SystemOnePolicy(**dict(system_one_policy))
    system_one_policy_dict = system_one_policy.to_dict()
    effective_actions = effective_action_registry(action_registry)
    domains = list(domains or [])
    pipeline = load_pipeline()
    profiles = pipeline["profiles"]
    aliases = pipeline["aliases"]
    stages = pipeline["stages"]

    routing_profiles, routing_pack_hash = load_routing_profiles(
        routing_profile_path, sharing_verifier=sharing_verifier)
    routing_profile = ({"routing_pack_version": "1.0.0",
                        "semantic_hash": routing_pack_hash,
                        "profiles": routing_profiles}
                       if len(routing_profiles) != 1
                       else (routing_profiles[0] if routing_profiles else None))
    routing_plugin = _load_document(ROOT / "plugin.json", _load_json)
    routing_pack_for_context = load_guard_pack()
    task_workflows = resolve_task_workflows(
        workflow_port=workflow_port,
        cow_root=cow_root,
        namespace_id=namespace_id,
        application_id=application_id,
        application_instance_id=application_instance_id,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        parent_session_id=parent_session_id,
        task_id=task_id,
        attempt_id=attempt_id,
        interaction_id=interaction_id,
        subject_ref=subject_ref,
        shape=shape,
        domains=domains,
        phase=phase,
        operation=operation,
        trigger=trigger,
        observation_context=observation_context,
        requested_flows=requested_flows,
        statework_subject_refs=statework_subject_refs,
        packet_store=packet_store,
        allow_missing_required=allow_missing_required,
        missing_required=missing_required,
    )
    workflow_eligibility = task_workflows.eligibility
    routing_context = {
        "adopted_profile_hashes": tuple(adopted_profile_hashes),
        "task_id": task_id,
        "session_id": None,
        "attempt_id": attempt_id,
        "agent_instance_id": agent_instance_id,
        "namespace_id": namespace_id, "application_id": application_id,
        "application_version": application_version,
        "application_instance_id": application_instance_id,
        "provider_id": provider_id, "model_id": model_id,
        "model_revision": model_revision,
        "model_capability_hash": model_capability_hash,
        "model": model, "harness": harness, "harness_id": harness,
        "harness_version": harness_version, "toolset": toolset,
        "task_family": shape, "task_shape": shape, "domain_tags": domains,
        "work_mode": goal_contract_dict.get("work_mode"),
        "deliverable_kind": goal_contract_dict.get("deliverable_kind"),
        "goal_contract_hash": goal_contract_hash,
        "acceptance_criterion_kinds": sorted({
            item.get("kind") for item in goal_contract_dict.get("acceptance_criteria", [])
            if item.get("kind")}),
        "verification_policy": goal_contract_dict.get("verification_policy"),
        "phase": phase, "environment": os.environ.get("CFW_ENVIRONMENT", "runtime"),
        "statework_versions": dict(workflow_eligibility.workflow_versions),
        "framework_version": routing_plugin.get("version", "unknown"),
        "guard_pack_hash": routing_pack_for_context.get("semantic_hash"),
        "role": role,
        "parent_decision_ref": parent_decision_ref,
        "shared_evidence_refs": shared_evidence_refs,
        "delegation_topology": delegation_topology,
        "premise_digest": premise_digest,
        "failure_attribution": failure_attribution,
        "interaction_id": interaction_id,
        "delegation_id": delegation_id,
    }
    routing_context["comparison_context_hash"] = comparison_context_hash(routing_context)
    routing_adjustments = applicable_adjustments(routing_profiles, routing_context)
    advice_receipt: Optional[Dict[str, Any]] = None
    routing_experiment = None
    routing_cohort = None
    candidate_profile_hash = None
    candidate_adjustment_applied = False
    comparison_hash = comparison_context_hash(routing_context)
    if routing_experiment_plan:
        routing_experiment_plan = validate_experiment_plan(routing_experiment_plan)
        routing_cohort = assign_experiment_cohort(
            routing_experiment_plan,
            f"{agent_instance_id}:{task_id}:{attempt_id}", routing_context)
        candidate_profile_hash = routing_experiment_plan.get("candidate_profile_hash")
        candidate_adjustment = dict(routing_experiment_plan.get("candidate_adjustment") or {})
        if routing_cohort == "treatment":
            candidate_adjustment_applied = True
            routing_adjustments.append({
                **candidate_adjustment,
                "profile_id": f"experiment:{routing_experiment_plan.get('experiment_id')}",
                "profile_hash": candidate_profile_hash,
                "experiment_id": routing_experiment_plan.get("experiment_id"),
                "experiment_plan_hash": routing_experiment_plan.get("plan_hash"),
                "candidate_adjustment_applied": True,
            })
        routing_experiment = {
            **routing_experiment_plan,
            "routing_cohort": routing_cohort,
            "eligible": routing_cohort is not None,
            "cohort_policy_identity": (
                routing_experiment_plan.get("treatment_policy_identity")
                if routing_cohort == "treatment"
                else routing_experiment_plan.get("control_policy_identity")
                if routing_cohort in {"control", "holdout"} else None),
            "candidate_profile_hash": candidate_profile_hash,
            "candidate_adjustment_applied": candidate_adjustment_applied,
            "comparison_context_hash": comparison_hash,
        }

    if shape not in profiles:
        raise ValueError(f"unknown task shape {shape!r}; expected one of {sorted(profiles)}")

    profile = expand(profiles[shape], aliases)
    # Build the host-computed eligibility snapshot before asking DP.  DP may
    # express preferences over these choices, but it never supplies them.
    eligible_routes = [{"route_type": "stage", "route": route} for route in profile]
    eligible_routes.extend(dict(item) for item in workflow_eligibility.eligible_routes)
    for guard in routing_pack_for_context.get("always_on", []) + routing_pack_for_context.get("guards", []):
        eligible_routes.append({"route_type": "guard", "route": str(guard.get("key") or guard.get("id") or "")})
    for action_id in effective_actions:
        eligible_routes.append({"route_type": "tool_policy", "route": str(action_id), "action_id": str(action_id)})
    static_policy_hash = hashlib.sha256(_stable_json({
        "profile": profile, "aliases": aliases, "stages": stages,
    }).encode("utf-8")).hexdigest()
    eligible_choice_hash = hashlib.sha256(_stable_json(eligible_routes).encode("utf-8")).hexdigest()
    advice_result = resolve_task_advice(
        behavioral_advisor=behavioral_advisor,
        routing_context=routing_context,
        eligible_routes=eligible_routes,
        shape=shape,
        framework_version=str(routing_context["framework_version"]),
        static_policy_hash=static_policy_hash,
        eligible_choice_hash=eligible_choice_hash,
        routing_adjustments=routing_adjustments,
        stages=stages,
    )
    routing_context = advice_result.routing_context
    routing_adjustments = advice_result.routing_adjustments
    advice = advice_result.advice
    bounded = advice_result.bounded_adjustments
    advice_receipt = advice_result.advice_receipt
    workflow_plan = resolve_prepared_task_workflows(
        task_workflows,
        routing_adjustments=tuple(dict(item) for item in routing_adjustments),
        statework_subject_refs=statework_subject_refs,
        subject_ref=subject_ref,
        domains=domains,
        phase=phase,
        requested_flows=requested_flows,
    )
    chain = list(workflow_plan.execution_order)
    enriched_stateworks = list(workflow_plan.active_workflows)
    statework_capsules = dict(workflow_plan.capsules)
    statework_registry = dict(workflow_plan.registry)
    statework_available = workflow_plan.available

    task_kernel = compose_task_kernel(
        profile=profile,
        stages=stages,
        aliases=aliases,
        stateworks=enriched_stateworks,
        routing_adjustments=routing_adjustments,
    )
    kernel_ordered = task_kernel.ordered
    kernel_instructions = task_kernel.instructions

    task_guards = resolve_task_guards(
        pack=routing_pack_for_context,
        stateworks=enriched_stateworks,
        domains=domains,
        shape=shape,
        model=model,
        harness=harness,
        trigger=trigger,
        task_id=task_id,
        routing_adjustments=routing_adjustments,
        statework_registry_version=statework_registry.get("version"),
        statework_available=statework_available,
        behavior_contract_path=LOCAL_CONTRACT_ROOT / "behavior-event.schema.json",
        load_document=_load_document,
        load_json=_load_json,
    )
    pack = task_guards.pack
    statework_ids = task_guards.statework_ids
    suppressed_guard_routes = task_guards.suppressed_routes

    # Policy hashes over actual inputs, not plugin.json alone.
    plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    framework_version = plugin.get("version", "unknown")
    pipeline_hash = sha256(ROOT / "shared" / "pipeline.yaml")
    kernel_hash = sha256(ROOT / "shared" / "runtime-kernel.yaml")
    capsule_hashes: Dict[str, str] = {}
    for name in ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD", "SISPIS"]:
        capsule = ROOT / name / "runtime.md"
        if capsule.exists():
            capsule_hashes[name] = sha256(capsule)
    cal_path = ROOT / "SISPIS" / "references" / "signal-calibration.yaml"
    sispis_calibration_hash = sha256(cal_path) if cal_path.exists() else ""
    signal_schema_path = ROOT / "shared" / "signal.schema.json"
    signal_registry_path = ROOT / "shared" / "signal-registry.json"
    behavior_event_schema_path = LOCAL_CONTRACT_ROOT / "behavior-event.schema.json"
    policy_documents: Dict[str, Any] = {
        "sispis_calibration": _load_document(cal_path, load_yaml),
        "signal_schema": _load_document(signal_schema_path, _load_json),
        "signal_registry": _load_document(signal_registry_path, _load_json),
        "behavior_event_schema": _load_document(behavior_event_schema_path, _load_json),
        "validator_registry_hash": validator_registry_hash(),
        "handler_registry_hash": handler_registry_hash(),
        "action_contract_hash": action_contract_hash(effective_actions),
        "runtime_behavior_hash": runtime_behavior_hash(),
        "routing_profile": routing_profile,
        "routing_experiment": routing_experiment,
    }
    behavior_schema = policy_documents["behavior_event_schema"]
    behavior_schema_version = str(
        behavior_schema.get("properties", {}).get("schema_version", {}).get("const", ""))
    if not behavior_schema_version:
        raise SnapshotIntegrityError("behavior-event schema has no pinned schema_version")
    policy_documents["behavior_event_schema_version"] = behavior_schema_version
    policy_documents["runtime_build_manifest"] = runtime_build_manifest(effective_actions)
    policy_documents["runtime_build_manifest_hash"] = (
        policy_documents["runtime_build_manifest"]["manifest_hash"])
    policy_documents["statework_workflows"] = {
        item["id"]: {"engine_kind": item.get("workflow_engine_kind"),
                     "contract_version": item.get("workflow_contract_version"),
                     "contract_hash": item.get("workflow_contract_hash")}
        for item in enriched_stateworks
    }
    statework_pins = tuple(
        StateWorkPin(id=item["id"], version=item["version"],
                     domain=str(item.get("domain") or ""),
                     purpose=str(item.get("purpose") or ""),
                     manifest_hash=item["manifest_hash"], runtime_hash=item["runtime_hash"],
                     selected_flow=(tuple(item["selected_flow"])
                                    if item["selected_flow"] else None),
                     selected_specialists=tuple(
                         tuple(pair) for pair in item["selected_specialists"]),
                     workflow_engine_kind=str(item.get("workflow_engine_kind") or "state_machine"),
                     workflow_contract_version=str(item.get("workflow_contract_version") or ""),
                     workflow_contract_hash=str(item.get("workflow_contract_hash") or ""),
                     flow_selection=item.get("flow_selection", {}),
                     subject_ref=item.get("subject_ref"))
        for item in enriched_stateworks
    )

    unique_guards = task_guards.unique_guards
    eligible_guard_keys = task_guards.eligible_keys
    guard_assignments = task_guards.assignments
    assigned_guard_keys = task_guards.assigned_keys

    handoff_plan = []
    for sw in chain:
        packet = sw.get("handoff") or (sw.get("emits") or [""])[0]
        handoff_plan.append({"producer": sw.get("id"), "packet": packet})
    core_capsules = injected_capsules_for(kernel_ordered)
    capsules = {stage: (text, sha256_bytes(text.encode("utf-8")))
                for stage, text in core_capsules.items()}
    capsules.update(statework_capsules)
    capsule_pins = tuple(
        CapsulePin(owner=stage.rsplit(":", 1)[-1].split("_")[0].upper(),
                   stage=stage, version="2.0.0", content_hash=content_hash)
        for stage, (_, content_hash) in sorted(capsules.items())
    )
    guard_pins = tuple(
        GuardPin(guard_key=guard.get("key") or guard["id"],
                 rule_hash=semantic_guard_hash(guard),
                 status_at_resolution=guard.get("status", "active"))
        for guard in unique_guards
    )
    effective_policy = EffectivePolicy(
        profile=shape, kernel=tuple(kernel_ordered), capsules=capsule_pins,
        stateworks=statework_pins, guards=guard_pins, model=model, model_id=model_id,
        provider_id=provider_id, model_revision=model_revision,
        model_capability_hash=model_capability_hash, harness=harness,
        harness_version=harness_version, toolset=toolset,
        namespace_id=namespace_id, application_id=application_id,
        application_version=application_version,
        application_instance_id=application_instance_id,
        trigger=trigger, pipeline_hash=pipeline_hash,
        runtime_kernel_hash=kernel_hash,
        calibration_hash=sispis_calibration_hash,
        system_one_policy=system_one_policy_dict,
        subject_ref=subject_ref,
        goal_contract_hash=goal_contract_hash,
        signal_schema_hash=sha256(signal_schema_path),
        signal_registry_hash=sha256(signal_registry_path),
        behavior_event_schema_hash=sha256(behavior_event_schema_path),
        behavior_event_schema_version=behavior_schema_version,
        runtime_behavior_hash=policy_documents["runtime_behavior_hash"],
        validator_registry_hash=policy_documents["validator_registry_hash"],
        handler_registry_hash=policy_documents["handler_registry_hash"],
        action_contract_hash=policy_documents["action_contract_hash"],
        runtime_build_manifest_hash=policy_documents["runtime_build_manifest"]["manifest_hash"],
        routing_profile_hash=hashlib.sha256(
            f"{routing_pack_hash}:{routing_experiment_plan.get('plan_hash', '')}:{routing_cohort or ''}".encode()
        ).hexdigest() if routing_experiment_plan else routing_pack_hash,
        parent_decision_ref=parent_decision_ref,
        shared_evidence_refs=tuple(shared_evidence_refs),
        delegation_topology=delegation_topology,
        premise_digest=premise_digest,
        failure_attribution=failure_attribution,
        adopted_profile_hashes=adopted_profile_hashes,
        shared_workflow_membership=shared_workflow_membership,
        shared_workflow_memberships=shared_workflow_memberships,
        statework_subject_refs=statework_subject_refs,
        policy_revocation_revision=policy_revocation_revision,
        revocation_mode=revocation_mode,
    )
    frozen_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    instruction_words = sum(len(str(k["activation"] or "").split()) + sum(len(str(o).split()) for o in k["outputs"]) for k in kernel_instructions)
    instruction_words += sum(
        len(str(g.get("rule", "")).split())
        for g in unique_guards
        if enforcement_for_guard(g).get("mode") == "prompt")
    instruction_words += sum(len(text.split()) for text, _hash in capsules.values())

    policy_hash = compute_policy_hash(effective_policy)
    execution_plan = tuple({
        "segment_id": f"segment-{index}",
        "index": index,
        "statework_id": item.get("id"),
        "subject_ref": statework_subject_refs.get(str(item.get("id")), subject_ref),
        "input_packet": ((item.get("inputs") or {}).get("required") or [None])[0]
            if isinstance(item.get("inputs"), dict) else None,
        "output_packet": (item.get("handoff") or (item.get("emits") or [None])[0]),
        "selected_flow": item.get("selected_flow"),
        "exclusive_group": item.get("exclusive_group"),
    } for index, item in enumerate(enriched_stateworks))
    if not execution_plan:
        execution_plan = ({"segment_id": "segment-0", "index": 0,
                           "statework_id": None, "subject_ref": subject_ref,
                           "input_packet": None, "output_packet": None},)
    if advice_receipt is not None:
        selected_routes = [{"route_type": "stage", "route": str(stage), "selection_reason": "effective_kernel"} for stage in kernel_ordered]
        selected_routes.extend({"route_type": "statework", "route": str(item.get("id")), "selection_reason": "effective_statework"} for item in enriched_stateworks)
        for item in enriched_stateworks:
            if item.get("selected_flow"):
                selected_routes.append({"route_type": "flow", "route": str(Path(item["selected_flow"][0]).parent.name), "selection_reason": "effective_flow"})
        advice_receipt["final_decision"] = {"policy_hash": compute_policy_hash(effective_policy), "selected_routes": selected_routes, "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    snapshot = PolicySnapshot(
        task_id=task_id,
        kernel=tuple(kernel_ordered),
        stateworks=statework_pins,
        guard_pack_version=pack["pack_version"],
        guard_pack_hash=pack["semantic_hash"],
        framework_version=framework_version,
        framework_hash=sha256(ROOT / "plugin.json"),
        model=model,
        model_id=model_id,
        provider_id=provider_id,
        model_revision=model_revision,
        model_capability_hash=model_capability_hash,
        harness=harness,
        harness_version=harness_version,
        toolset=toolset,
        namespace_id=namespace_id,
        application_id=application_id,
        application_version=application_version,
        application_instance_id=application_instance_id,
        frozen_at=frozen_at,
        policy_hash=policy_hash,
        subject_ref=subject_ref,
        goal_contract=goal_contract_dict,
        goal_contract_hash=goal_contract_hash,
        system_one_policy=system_one_policy_dict,
        agent_id=agent_id,
        phase=phase,
        domain_tags=tuple(domains),
        task_shape=shape,
        operation=operation,
        trigger=trigger,
        execution_plan=execution_plan,
        agent_instance_id=agent_instance_id,
        attempt_id=attempt_id,
        interaction_id=interaction_id,
        parent_session_id=parent_session_id,
        delegator_agent_id=delegator_agent_id,
        delegate_agent_id=delegate_agent_id,
        delegation_id=delegation_id,
        role=role,
        parent_decision_ref=parent_decision_ref,
        shared_evidence_refs=tuple(shared_evidence_refs),
        delegation_topology=delegation_topology,
        premise_digest=premise_digest,
        failure_attribution=failure_attribution,
        adopted_profile_hashes=tuple(adopted_profile_hashes),
        shared_workflow_membership=shared_workflow_membership,
        shared_workflow_memberships=tuple(shared_workflow_memberships),
        statework_subject_refs=statework_subject_refs,
        policy_revocation_revision=policy_revocation_revision,
        revocation_mode=revocation_mode,
        contracts=policy_documents,
    )

    bundle = RuntimeBundle(
        bundle_version="2.0.0",
        task_id=task_id,
        profile=shape,
        kernel=kernel_ordered,
        kernel_instructions=kernel_instructions,
        stateworks=enriched_stateworks,
        handoff_plan=handoff_plan,
        guard_pack={
            "version": pack["pack_version"],
            "hash": pack["semantic_hash"],
            "status": pack.get("guard_pack_status", "valid"),
            "eligible_guard_keys": eligible_guard_keys,
            "assigned_guard_keys": assigned_guard_keys,
            "guard_assignments": guard_assignments,
            "suppressed_guard_keys": sorted(suppressed_guard_routes),
            "routing_profile": routing_profile,
            "routing_adjustments": routing_adjustments,
            "advice_exchange": advice_receipt,
            "routing_experiment": routing_experiment,
            "structural_handlers": {
                (g.get("key") or g["id"]): enforcement_for_guard(g)
                for g in unique_guards
                if enforcement_for_guard(g).get("mode") != "prompt"
            },
            "guards": [
                {"id": g["id"], "key": g.get("key") or g["id"],
                 "family": g.get("family") or g["id"], "version": g.get("version", "1"),
                 "status": g.get("status", "active"),
                 "target_behavior": g.get("target_behavior"),
                 "rule": g.get("rule", g.get("target_behavior", "")),
                 "scope": g.get("scope"),
                 "trigger": g.get("trigger"),
                 "evaluator": g.get("evaluator"),
                 "enforcement_key": g.get("enforcement_key"),
                 "enforcement": enforcement_for_guard(g)}
                for g in unique_guards
            ],
        },
        pinned=snapshot.to_dict(),
        policy=effective_policy,
        capsule_hashes=capsule_hashes,
        injected_capsules=tuple(capsules.items()),
        instruction_words=instruction_words,
        execution_plan=execution_plan,
        statework_root=str(cow_root.resolve()),
        action_registry=effective_actions,
    )
    return bundle
