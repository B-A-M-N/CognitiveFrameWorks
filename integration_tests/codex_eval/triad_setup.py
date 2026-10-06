"""Build the CFW/CSW/DP host boundary for one disposable trial."""
from __future__ import annotations

import hashlib
import os
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_app_server_adapter import (
    CodexActionBinding,
    CodexAppServerAdapter,
    CodexValidatorBinding,
)
from codex_app_server_dp_bridge import DigitalPsychologyBridgeHost
from codex_host.launch import CodexLaunchProfile
from lib.behavior_state import GoalItemSnapshot, GoalSetSnapshot
from lib.host import HostControlBridge, MCPControlClient
from lib.runtime.adapters.cfw import CFWRuntimeAdapter
from lib.runtime.adapters.csw import CSWRuntimeAdapter, CSWWorkflowBinding
from lib.runtime.contracts import ActionCandidate, RuntimeIdentity, RuntimeObservation
from runtime_contract.actions import HostExecutionContext, ProtectedResourcePolicy
from runtime_contract.task import (
    AcceptanceCriterion,
    TaskGoalContract,
    TaskRequest,
    VerificationPolicy,
)
from runtime_host.host_api import CognitiveRuntime

from .common import CSW, DP
from .csw_setup import (
    _complete_gitter_workflow,
    _make_csw,
    _prepare_getter_workflow,
)
from .report import _record_app_server_notification


@dataclass
class TriadSetup:
    adapter: CodexAppServerAdapter
    request: TaskRequest
    authority: Any
    with_csw: bool
    with_dp: bool
    requested_statework_ids: tuple[str, ...]
    cfw_gate_receipts: list[dict[str, Any]]
    dp_observation_records: list[dict[str, Any]]
    csw_workflow_identities: dict[str, Mapping[str, Any]]
    csw_workflow_results: list[dict[str, Any]]
    getter_prepared: dict[str, Any]
    cognitive_host: Any
    server_request_counts: Counter[str]


def _build_treatment_host(
        *, condition: str, workspace: Path, root: Path, model: str, trial_id: str,
        contract: Mapping[str, Any], actions: tuple[CodexActionBinding, ...],
        validators: tuple[CodexValidatorBinding, ...], notifications: list[dict[str, Any]],
        launch_profile: CodexLaunchProfile,
        launch_environment: Mapping[str, str],
        dynamic_tool_probe: bool = False) -> TriadSetup:
    criterion_ids = tuple(str(item["id"]) for item in contract["criteria"])
    server_request_counts: Counter[str] = Counter()
    goal = TaskGoalContract(
        objective=str(contract["objective"]),
        deliverable_kind=str(contract["deliverable_kind"]),
        work_mode=str(contract["work_mode"]),
        task_complexity=str(contract["task_complexity"]),
        non_goals=tuple(str(item) for item in contract["non_goals"]),
        acceptance_criteria=tuple(AcceptanceCriterion(
            str(item["id"]), str(item["claim"]), "code_behavior",
            required=True, verification_strength="host_validator",
            required_boundaries=(() if dynamic_tool_probe else
                                 (f"validator:{item['validator']}",)))
            for item in contract["criteria"]),
        verification_policy=VerificationPolicy(
            max_verification_actions_without_artifact_change=max(
                2, 2 * len(validators))),
    )
    with_csw = condition in {"cfw-csw", "triad", "triad-multipath"}
    with_dp = condition in {"triad", "triad-multipath"}
    if condition == "triad-multipath":
        requested_statework_ids = ("getter",)
    elif with_csw or dynamic_tool_probe:
        requested_statework_ids = ("gitter",)
    else:
        requested_statework_ids = ()
    subject_ref = f"repo:{trial_id}" if with_csw else None
    request = TaskRequest(
        task_id=trial_id, application_id="generic-codex-appserver-coding-eval",
        subject_ref=subject_ref,
        shape="implement",
        domain_tags=("repository_state",) if with_csw else (),
        operation="repair",
        requested_statework_ids=requested_statework_ids,
        statework_subject_refs=({"getter": subject_ref}
                                if requested_statework_ids and subject_ref else {}),
        model=model, harness="codex-app-server", harness_version="1",
        toolset="codex-app-server-coding-v1", goal_contract=goal)
    authority = state_authority = workflow_port = None
    if with_csw:
        authority, state_authority, workflow_port = _make_csw(request, root)

    src_root = workspace / "src"
    protected = ProtectedResourcePolicy(
        protected_roots=tuple(str(path.resolve()) for path in (
            workspace / "tests", workspace / "README.md",
            workspace / ".cognitive-demo.json")),
        authorized_write_roots=(str(src_root.resolve()),))
    host_context = HostExecutionContext(
        principal="runtime", task_scope=trial_id,
        filesystem_scope=(str(src_root.resolve()),), task_id=trial_id,
        protected_resources=protected)
    runtime = CognitiveRuntime(host_context=host_context,
        action_registry={item.action_binding_id: item.descriptor for item in actions},
        workflow_port=workflow_port)
    dp_env = None
    if with_dp:
        private_state = root / "dp-private-state"
        private_state.mkdir(mode=0o700)
        dp_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "DIGITALPSYCHOLOGY_ROOT": str(DP),
            "COGNITIVE_STATEWORK_ROOT": str(CSW),
            "DIGITALPSYCHOLOGY_STATE_ROOT": str(private_state),
            "DIGITALPSYCHOLOGY_EVENT_DB": str(private_state / "events.sqlite3"),
            "DIGITALPSYCHOLOGY_BEHAVIOR_STATE_DB": str(private_state / "behavior.sqlite3"),
            "DIGITALPSYCHOLOGY_SYSTEM_ONE_MODE": "disabled",
        }

    bridge_refs: list[HostControlBridge] = []
    cfw_gate_receipts: list[dict[str, Any]] = []
    verified: set[str] = set()
    goal_revision = {"value": 0}
    criterion_by_validator = {str(item["validator"]): str(item["id"])
                              for item in contract["criteria"]}
    dp_observation_records: list[dict[str, Any]] = []
    csw_workflow_identities: dict[str, Mapping[str, Any]] = {}
    csw_workflow_results: list[dict[str, Any]] = []
    getter_prepared: dict[str, Any] = {}

    def current_goal_set() -> GoalSetSnapshot:
        return GoalSetSnapshot(revision=goal_revision["value"], items=tuple(
            GoalItemSnapshot(criterion_id=criterion_id,
                             status="VERIFIED" if criterion_id in verified else "READY")
            for criterion_id in criterion_ids))

    def bridge_factory(*, request: TaskRequest, task_handle: Any,
                       host_handle: Any, session_id: str,
                       **kwargs: Any) -> HostControlBridge:
        behavior = task_handle.digital_psychology_adapter()
        workflow_ids = behavior.workflow_authority_identities()
        if not workflow_ids:
            raise RuntimeError("CFW task did not bind a CSW workflow authority")

        class TracedBehavior:
            @staticmethod
            def record_gate(stage: str, result: Mapping[str, Any]) -> None:
                nested = result.get("gate_receipt")
                receipt = nested if isinstance(nested, Mapping) else result
                action_digest = receipt.get("action_digest")
                cfw_gate_receipts.append({
                    "stage": stage,
                    "action_request_id": str(
                        result.get("action_request_id") or "")[:128],
                    "action_digest": (str(action_digest)[:64]
                                      if isinstance(action_digest, str) else ""),
                    "decision": str(receipt.get("decision") or "")[:32],
                    "reason_codes": list(receipt.get("reason_codes") or ())[:8],
                    "cfw_revision": behavior.lifecycle_revision,
                })

            def preflight_action(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
                result = behavior.preflight_action(action)
                self.record_gate("preflight", result)
                return result

            def authorize_action(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
                result = behavior.authorize_action(action)
                self.record_gate("final", result)
                return result

            def __getattr__(self, name: str) -> Any:
                return getattr(behavior, name)

        cfw_port = CFWRuntimeAdapter(TracedBehavior())
        subject = request.subject_ref or request.task_id
        identity = RuntimeIdentity(
            namespace_id=request.namespace_id, application_id=request.application_id,
            application_instance_id=request.application_instance_id,
            agent_id=request.agent_id, agent_instance_id=task_handle.agent_instance_id,
            session_id=session_id, task_id=request.task_id,
            attempt_id=request.attempt_id,
            workflow_id=str(workflow_ids[min(workflow_ids)]["workflow_id"]),
            subject_ref=subject, segment_id="segment-0", behavioral_subject=subject,
            goal_contract_hash=str(task_handle.bundle.pinned.get(
                "goal_contract_hash") or "") or None)
        csw_bindings = {}
        for statework_id, cfw_identity in workflow_ids.items():
            contract = authority.workflow_contract_identity(statework_id)
            csw_identity = {
                key: cfw_identity[key] for key in (
                    "namespace_id", "application_id", "application_instance_id",
                    "workflow_id", "subject_ref")}
            csw_identity.update({
                "statework_id": statework_id,
                "contract_version": contract["contract_version"],
            })
            csw_workflow_identities[statework_id] = csw_identity
            csw_bindings[statework_id] = CSWWorkflowBinding(
                identity=csw_identity, contract_hash=contract["contract_hash"])
        if set(csw_bindings) != set(workflow_ids):
            raise RuntimeError(
                "DP binding set differs from CFW workflow set "
                f"(DP={sorted(csw_bindings)}, CFW={sorted(workflow_ids)})")
        csw_workflow_identities.update({
            str(statework_id): {**dict(identity),
                                "statework_id": str(statework_id)}
            for statework_id, identity in workflow_ids.items()})
        if condition == "triad-multipath":
            if set(workflow_ids) != {"gitter", "getter"}:
                raise RuntimeError(
                    "host request did not resolve Getter with its Gitter dependency")
            gitter_result = _complete_gitter_workflow(
                authority, csw_workflow_identities["gitter"], workspace,
                request, host_handle)
            csw_workflow_results.append(gitter_result)
            # Gitter's CFW handoff closes its execution segment; Getter must
            # run in the next pinned segment in the same CFW task.
            host_handle.advance_segment()
            host_handle.consume_handoff(
                str(gitter_result["cfw_handoff_packet_id"]),
                expected_packet_type="repository_truth_packet",
                expected_subject_ref=request.subject_ref or request.task_id,
                observation_context={
                    "repository_path": str(workspace.resolve()),
                    "repository_freshness": gitter_result["repository_freshness"],
                })
            getter_prepared.update(_prepare_getter_workflow(
                authority, csw_workflow_identities["getter"], workspace,
                request, host_handle, gitter_result=gitter_result))
            csw_workflow_results.append(getter_prepared)
        csw_port = CSWRuntimeAdapter(authority, identity=identity,
                                     bindings=csw_bindings)
        bridge = HostControlBridge(
            identity=identity, objective=goal.objective,
            objective_criteria=criterion_ids, initial_goal_set=current_goal_set(),
            cfw=cfw_port, csw=csw_port,
            client=MCPControlClient(command=sys.executable,
                args=("-m", "lib.transport.control_mcp"), env=dp_env, cwd=DP),
            csw_authority_identity=csw_port.authority_identity())
        bridge_refs.append(bridge)
        return bridge

    def candidate_factory(*, action: Mapping[str, Any], binding: str,
                          invocation_id: str, **kwargs: Any) -> ActionCandidate:
        arguments = action.get("arguments") or {}
        if binding == "host_validator":
            validator_id = str(arguments.get("validator") or "")
            affected = ((criterion_by_validator[validator_id],)
                        if validator_id in criterion_by_validator else ())
            target = f"validator:{validator_id}"
            action_class = "test"
        elif binding == "write":
            affected = criterion_ids
            target = str(arguments.get("path") or "")
            action_class = "file_change"
        elif binding == "read":
            affected = criterion_ids if dynamic_tool_probe else ()
            target = str(arguments.get("path") or "")
            action_class = "read"
            if dynamic_tool_probe:
                dp_observation_records.append({"boundary": "pre_action"})
        else:
            raise RuntimeError("unrecognized coding evaluation action")
        return ActionCandidate(
            action_class=action_class,
            proposed_purpose=("Run the host-registered criterion validator"
                              if binding == "host_validator" else
                              "Inspect the task workspace" if binding == "read" else
                              "Repair the host-owned code criteria"),
            affected_criterion_ids=affected, action_id=invocation_id,
            tool_name="cfw_" + binding, operation=binding,
            target_ref_digest=hashlib.sha256(target.encode()).hexdigest(),
            mutating=binding == "write", reversible=binding == "write",
            relation_certainty="explicit" if affected else "unknown")

    def observation_factory(*, boundary: str, task_handle: Any,
                            bridge: HostControlBridge, **kwargs: Any
                            ) -> RuntimeObservation:
        status = {criterion_id: ("verified" if criterion_id in verified else "unmet")
                  for criterion_id in criterion_ids}
        result = kwargs.get("actual_result")
        validator_id = str(kwargs.get("validator_id") or "")
        if boundary == "post_action" and kwargs.get("binding") == "test":
            if isinstance(result, Mapping):
                validator_id = str(result.get("validator") or "")
                if (result.get("status") == "passed"
                        and validator_id in criterion_by_validator):
                    verified.add(criterion_by_validator[validator_id])
                    goal_revision["value"] += 1
                    status[criterion_by_validator[validator_id]] = "verified"
        elif boundary in {"validator", "pre_completion"}:
            raw_result = kwargs.get("validator_result")
            if boundary == "validator" and isinstance(raw_result, Mapping):
                validator_id = str(raw_result.get("validator") or "")
                if (raw_result.get("status") == "passed"
                        and validator_id in criterion_by_validator
                        and criterion_by_validator[validator_id] not in verified):
                    verified.add(criterion_by_validator[validator_id])
                    goal_revision["value"] += 1
                    status[criterion_by_validator[validator_id]] = "verified"
            if (boundary == "pre_completion"
                    and isinstance(raw_result, Mapping)
                    and raw_result.get("status") == "passed"):
                for item in criterion_ids:
                    if item not in verified:
                        verified.add(item)
                        goal_revision["value"] += 1
                        status[item] = "verified"
        current = current_goal_set()
        if boundary in {"validator", "pre_completion"}:
            goal_revision["value"] += 1
            current = GoalSetSnapshot(revision=goal_revision["value"],
                items=tuple(GoalItemSnapshot(criterion_id=item,
                    status="VERIFIED" if item in verified else "READY")
                    for item in criterion_ids))
            status = {item: ("verified" if item in verified else "unmet")
                      for item in criterion_ids}
        evidence_refs = ()
        if boundary in {"validator", "pre_completion"}:
            refs = kwargs.get("criterion_evidence_refs") or {}
            evidence_refs = tuple(ref for values in refs.values() for ref in values)[:16]
        completed_action = boundary == "post_action"
        observed_index = (max(1, bridge._action_index + 1) if completed_action
                          else max(0, bridge._action_index))
        expected_index = (max(1, bridge._action_index + 1)
                          if boundary in {"pre_action", "pre_action_rejected",
                                          "post_action"}
                          else max(0, bridge._action_index))
        dp_observation_records.append({
            "boundary": boundary,
            "observed_action_index": observed_index,
            "current_host_action_index": bridge._action_index,
            "expected_action_index": expected_index,
        })
        return RuntimeObservation(
            action_index=observed_index,
            action_completed=completed_action,
            progress_made=(completed_action and isinstance(result, Mapping)
                           and result.get("status") == "passed"),
            artifact_changed=(completed_action and kwargs.get("binding") == "write"
                              and isinstance(result, Mapping)
                              and result.get("changed") is True),
            actions_since_goal_progress=0 if any(
                item in verified for item in criterion_ids) else 1,
            equivalent_retry_count=0, goal_set=current,
            criterion_status=status, evidence_refs=evidence_refs)

    cognitive_host = None
    if with_dp:
        cognitive_host = DigitalPsychologyBridgeHost(
            bridge_factory=bridge_factory, candidate_factory=candidate_factory,
            observation_factory=observation_factory)

    adapter = CodexAppServerAdapter(
        request=request, action_bindings=actions, validator_bindings=validators,
        model=model, cwd=workspace, runtime=runtime,
        cognitive_host=cognitive_host,
        runtime_mode="combined_enforcing" if with_csw else "standalone_enforcing",
        task_start_options=({"state_authority": state_authority}
                            if with_csw else None),
        event_handler=lambda method, params: _record_app_server_notification(
            notifications, method, params),
        launch_profile=launch_profile, launch_environment=launch_environment)
    # The receipt stream is present in every treatment condition. DP uses the
    # TracedBehavior wrapper; standalone CFW records direct gate decisions.
    adapter._cfw_gate_receipts = cfw_gate_receipts

    return TriadSetup(
        adapter=adapter,
        request=request,
        authority=authority,
        with_csw=with_csw,
        with_dp=with_dp,
        requested_statework_ids=requested_statework_ids,
        cfw_gate_receipts=cfw_gate_receipts,
        dp_observation_records=dp_observation_records,
        csw_workflow_identities=csw_workflow_identities,
        csw_workflow_results=csw_workflow_results,
        getter_prepared=getter_prepared,
        cognitive_host=cognitive_host,
        server_request_counts=server_request_counts,
    )
