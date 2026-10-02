"""CFW runtime session startup and resume orchestration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional

from runtime_contract.actions import (
    ActionDescriptor, HostExecutionContext, action_contract_hash,
    default_action_registry, effective_action_registry,
)
from runtime_contract.convergence import ConvergenceController, SharedBranchLedger
from runtime_contract.evidence import EvidenceLedger
from runtime_contract.handlers import handler_registry_hash
from runtime_contract.telemetry import DurableTelemetryExporter
from runtime_contract.stateworks import ExecutionSegment, SegmentStatus
from runtime_contract.system_one import BehavioralJudge, SystemOneCapability, SystemOnePolicy
from runtime_session.host_authority import (
    PolicyRevocationAuthority, SharedWorkflowMembershipAuthority, StateAuthority,
)
from runtime_session.journal import _journal_identity_prefix, _restore_session
from runtime_session.lifecycle import (
    ExecutionIdentity, GateDecision, LifecycleController, PolicyEpochRecord,
)
from runtime_session.resolver_bridge import resolve_runtime
from runtime_session.session import RuntimeSession, TelemetrySink
from runtime_session.behavior_authority import StandaloneBehaviorAuthority
from runtime_session.telemetry import HostTelemetry

RUNTIME_MODES = {"standalone_advisory", "standalone_enforcing", "combined_enforcing"}
DEFAULT_ACTION_REGISTRY: Dict[str, ActionDescriptor] = default_action_registry()

def start(bundle: resolve_runtime.RuntimeBundle, *,
          telemetry_sink: Optional[TelemetrySink] = None,
          telemetry_exporter: Optional[DurableTelemetryExporter] = None,
          behavioral_judge: Optional[BehavioralJudge] = None,
          state_authority: Optional[StateAuthority] = None,
          policy_revocation_authority: Optional[PolicyRevocationAuthority] = None,
          membership_authority: Optional["SharedWorkflowMembershipAuthority"] = None,
          operator_read_model: Optional[Any] = None,
          runtime_mode: str = "standalone_enforcing",
          telemetry_disabled: bool = False,
          tool_hooks: Optional[List[Callable[[Dict[str, Any]], Optional[GateDecision]]]] = None,
          final_hooks: Optional[List[Callable[[Dict[str, Any]], None]]] = None,
          action_registry: Optional[Mapping[str, ActionDescriptor]] = None,
          host_context: Optional[HostExecutionContext] = None,
          session_nonce: Optional[str] = None,
          session_id: Optional[str] = None,
          policy_epoch_record: Optional[PolicyEpochRecord] = None,
          resume: bool = False) -> RuntimeSession:
    """Inject selected runtime bytes, attach default host telemetry, and
    persist the immutable snapshot. Only tests may explicitly disable the
    sink; ordinary callers cannot silently turn it off."""
    if runtime_mode not in RUNTIME_MODES:
        raise ValueError(f"unknown runtime mode {runtime_mode!r}")
    supplied_context = host_context or HostExecutionContext()
    pinned_contracts = bundle.pinned.get("contracts", {})
    pinned_runtime_hash = pinned_contracts.get("runtime_behavior_hash")
    if pinned_runtime_hash != resolve_runtime.runtime_behavior_hash():
        raise resolve_runtime.SnapshotIntegrityError(
            "runtime enforcement code changed after policy composition")
    system_one_policy = SystemOnePolicy(**dict(bundle.pinned.get("system_one_policy") or {}))
    if dict(bundle.policy.system_one_policy) != system_one_policy.to_dict():
        raise resolve_runtime.SnapshotIntegrityError(
            "System-1 policy differs from the task's pinned policy")
    if policy_epoch_record is not None:
        if (not isinstance(policy_epoch_record, PolicyEpochRecord)
                or policy_epoch_record.epoch_index < 1
                or policy_epoch_record.parent_policy_hash == policy_epoch_record.child_policy_hash
                or policy_epoch_record.child_policy_hash != bundle.pinned.get("policy_hash")
                or policy_epoch_record.route_type != "flow"
                or policy_epoch_record.reason != "dynamic_replan"
                or policy_epoch_record.decision_source not in {
                    "metacognition", "operator", "learned_profile", "semantic_advisory"}
                or not policy_epoch_record.decision_ref
                or (policy_epoch_record.decision_source == "semantic_advisory"
                    and not policy_epoch_record.semantic_decision_ref)
                or policy_epoch_record.state_revision < 0):
            raise resolve_runtime.SnapshotIntegrityError(
                "child policy epoch record does not match the immutable runtime bundle")
    if behavioral_judge is not None:
        capability_method = getattr(behavioral_judge, "capability", None)
        if not callable(capability_method):
            raise ValueError("System-1 provider must expose a capability handshake")
        capability = capability_method()
        if isinstance(capability, Mapping):
            capability = SystemOneCapability(
                **{**dict(capability),
                   "supported_question_families": tuple(
                       capability.get("supported_question_families") or ())})
        if not isinstance(capability, SystemOneCapability):
            raise ValueError("System-1 provider returned an invalid capability handshake")
        pinned_capabilities = {
            "backend_id": system_one_policy.backend_id,
            "model_id": system_one_policy.model_id,
            "model_revision": system_one_policy.model_revision,
            "model_content_hash": system_one_policy.model_content_hash,
            "provider_protocol_hash": system_one_policy.provider_protocol_hash,
            "protocol_version": system_one_policy.protocol_version,
            "protocol_hash": system_one_policy.protocol_hash,
            "question_registry_hash": system_one_policy.question_registry_hash,
            "state_projection_schema_hash": system_one_policy.state_projection_schema_hash,
        }
        for field_name, expected in pinned_capabilities.items():
            if getattr(capability, field_name) != expected:
                raise ValueError(f"System-1 capability {field_name} differs from pinned policy")
        if capability.max_state_bytes < 32768:
            raise ValueError("System-1 provider cannot accept the maximum protocol frame")
        if not set(system_one_policy.enabled_question_families).issubset(
                capability.supported_question_families):
            raise ValueError("System-1 provider does not support every enabled question family")
        if capability.stateful_sessions and (
                not callable(getattr(behavioral_judge, "reset_identity", None))
                or not callable(getattr(behavioral_judge, "resync", None))):
            raise ValueError("stateful System-1 provider must implement reset and resync")
        provider_cache_size = getattr(behavioral_judge, "cache_size", None)
        if provider_cache_size is not None and int(provider_cache_size) != system_one_policy.cache_size:
            raise ValueError("injected System-1 cache size differs from pinned policy")
        provider_latency = getattr(behavioral_judge, "max_latency_ms", None)
        if (provider_latency is not None
                and int(provider_latency) > system_one_policy.max_latency_ms):
            raise ValueError("injected System-1 provider exceeds pinned latency cap")
    if (session_nonce is not None and supplied_context.session_nonce
            and session_nonce != supplied_context.session_nonce):
        raise ValueError("session nonce supplied by host context and resume request differ")
    session_nonce = supplied_context.session_nonce or session_nonce or f"nonce-{uuid.uuid4()}"
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", bundle.task_id)[:128] or "task"
    journal_root = resolve_runtime.runtime_state_dir() / "sessions"
    if resume and session_id is None:
        # Analytics identity is restored from the authority journal; it is
        # never regenerated from the security nonce or inferred from task ID.
        journal_db = journal_root / f"session-{_journal_identity_prefix(bundle)}-{safe_task}-{session_nonce}.sqlite3"
        if journal_db.exists():
            with sqlite3.connect(journal_db) as database:
                row = database.execute(
                    "SELECT event_json FROM authority_events ORDER BY seq LIMIT 1").fetchone()
            if row:
                session_id = json.loads(row[0]).get("session_id")
    session_id = session_id or f"analytics-{uuid.uuid4()}"
    if supplied_context.confirmation_grants and (
            supplied_context.task_id != bundle.task_id
            or supplied_context.policy_hash != bundle.pinned["policy_hash"]
            or supplied_context.session_nonce != session_nonce):
        raise ValueError("confirmation grants must be bound to this task policy and session")
    bound_context = replace(supplied_context, task_id=bundle.task_id,
                            policy_hash=bundle.pinned["policy_hash"],
                            session_nonce=session_nonce)
    resolved_action_registry = effective_action_registry(
        bundle.action_registry or DEFAULT_ACTION_REGISTRY)
    current_manifest = resolve_runtime.runtime_build_manifest(resolved_action_registry)
    pinned_manifest = pinned_contracts.get("runtime_build_manifest") or {}
    if (pinned_contracts.get("runtime_build_manifest_hash")
            != current_manifest.get("manifest_hash")
            or pinned_manifest != current_manifest):
        raise resolve_runtime.SnapshotIntegrityError(
            "runtime build manifest changed after policy composition")
    if action_registry is not None:
        supplied_registry = effective_action_registry(action_registry)
        if action_contract_hash(supplied_registry) != action_contract_hash(
                resolved_action_registry):
            raise ValueError(
                "start-time action registry differs from the registry pinned at resolve()")
    if action_contract_hash(resolved_action_registry) != bundle.policy.action_contract_hash:
        raise resolve_runtime.SnapshotIntegrityError(
            "host action registry changed after policy composition")
    session = RuntimeSession(bundle=bundle, injected={
        key: value[0] if isinstance(value, tuple) else value
        for key, value in bundle.injected_capsules
    }, ledger=EvidenceLedger(bundle.task_id),
       _action_registry=dict(resolved_action_registry),
       host_context=bound_context,
       execution_segments=tuple(
           ExecutionSegment(
               segment_id=str(item.get("segment_id")),
               index=int(item.get("index", index)),
               statework_id=item.get("statework_id"),
               subject_ref=item.get("subject_ref"),
               input_packet=item.get("input_packet"),
               output_packet=item.get("output_packet"),
               exclusive_group=item.get("exclusive_group"),
               lifecycle_stages=tuple(item.get("lifecycle_stages", ())),
           ) for index, item in enumerate(bundle.execution_plan)
       ) or (ExecutionSegment("segment-0", 0, None, bundle.policy.subject_ref),),
       session_nonce=session_nonce,
       session_id=session_id,
       behavior_authority=(StandaloneBehaviorAuthority(
           system_one_judge=behavioral_judge)
           if behavioral_judge is not None else None),
       agent_instance_id=bundle.pinned.get("agent_instance_id", bundle.pinned.get("agent_id", "agent")),
       attempt_id=bundle.pinned.get("attempt_id", "attempt-1"),
       interaction_id=bundle.pinned.get("interaction_id"),
       parent_session_id=bundle.pinned.get("parent_session_id"),
       delegator_agent_id=bundle.pinned.get("delegator_agent_id"),
       delegate_agent_id=bundle.pinned.get("delegate_agent_id"),
       delegation_id=bundle.pinned.get("delegation_id"),
       role=bundle.pinned.get("role"),
       parent_decision_ref=bundle.pinned.get("parent_decision_ref"),
       shared_evidence_refs=tuple(bundle.pinned.get("shared_evidence_refs") or ()),
       delegation_topology=bundle.pinned.get("delegation_topology"),
       premise_digest=bundle.pinned.get("premise_digest"),
       failure_attribution=bundle.pinned.get("failure_attribution") or "none",
       adopted_profile_hashes=tuple(bundle.pinned.get("adopted_profile_hashes") or ()),
       shared_workflow_membership=bundle.pinned.get("shared_workflow_membership"),
       shared_workflow_memberships=tuple(
           MappingProxyType(dict(item)) for item in
           bundle.pinned.get("shared_workflow_memberships", ())),
       policy_revocation_revision=int(bundle.pinned.get("policy_revocation_revision", 0) or 0),
       revocation_mode=str(bundle.pinned.get("revocation_mode") or "deferred"),
       policy_epoch_record=policy_epoch_record)
    session.metacognitive_state.ensure_criteria(bundle.pinned.get("goal_contract") or {})
    session._ledger_token = object()
    session.ledger = EvidenceLedger(bundle.task_id, writer_token=session._ledger_token)
    session.execution_identity = ExecutionIdentity(
        namespace_id=bundle.pinned["namespace_id"],
        application_instance_id=bundle.pinned["application_instance_id"],
        agent_instance_id=session.agent_instance_id,
        session_id=session.session_id, task_id=bundle.task_id,
        attempt_id=session.attempt_id, interaction_id=session.interaction_id)
    verification_policy = ((bundle.pinned.get("goal_contract") or {}).get(
        "verification_policy") or {})
    session.convergence = ConvergenceController(identity={
        "namespace_id": session.execution_identity.namespace_id,
        "application_instance_id": session.execution_identity.application_instance_id,
        "agent_instance_id": session.execution_identity.agent_instance_id,
        "session_id": session.execution_identity.session_id,
        "task_id": session.execution_identity.task_id,
        "attempt_id": session.execution_identity.attempt_id,
        "interaction_id": session.execution_identity.interaction_id,
    }, max_recovery_epochs_without_progress=int(
        verification_policy.get("max_recovery_epochs_without_goal_progress", 3) or 3),
        max_same_approach_retries=int(
            verification_policy.get("max_same_approach_retries", 2) or 2))
    if runtime_mode == "combined_enforcing":
        if state_authority is None:
            raise ValueError("combined_enforcing requires an injected shared StateAuthority")
        if not bundle.stateworks:
            raise ValueError("combined_enforcing requires at least one StateWork-backed task")
        if callable(getattr(state_authority, "bind_scope", None)):
            memberships = list(session.shared_workflow_memberships)
            if session.shared_workflow_membership and not any(
                    dict(item) == dict(session.shared_workflow_membership)
                    for item in memberships):
                memberships.append(MappingProxyType(
                    dict(session.shared_workflow_membership)))
            expected_subjects = dict(bundle.pinned.get("statework_subject_refs") or {})
            authorized_memberships: Dict[str, Mapping[str, Any]] = {}
            for membership in memberships:
                statework_id = str(membership.get("statework_id") or "")
                if (not statework_id or statework_id not in {
                        str(item.get("id")) for item in bundle.stateworks}):
                    raise ValueError("shared workflow membership names an inactive StateWork")
                expected_subject = expected_subjects.get(statework_id, bundle.policy.subject_ref)
                for field, expected in (
                    ("namespace_id", bundle.pinned["namespace_id"]),
                    ("application_id", bundle.pinned["application_id"]),
                    ("application_instance_id", bundle.pinned["application_instance_id"]),
                    ("subject_ref", expected_subject),
                ):
                    if membership.get(field) != expected:
                        raise ValueError(
                            f"shared workflow membership {field} differs from its StateWork binding")
                if statework_id in authorized_memberships:
                    raise ValueError("a StateWork cannot have multiple workflow memberships")
                if membership_authority is None or not membership_authority.validate(
                        membership, session=session, operation="transition"):
                    raise PermissionError("shared workflow membership is not host-authorized")
                authorized_memberships[statework_id] = membership
            if len(authorized_memberships) == 1:
                workflow_id = str(next(iter(authorized_memberships.values()))["workflow_id"])
            else:
                workflow_id = (
                    f"private:{bundle.pinned['namespace_id']}:"
                    f"{bundle.pinned['application_instance_id']}:{session.agent_instance_id}")
            state_authority = state_authority.bind_scope(
                namespace_id=bundle.pinned["namespace_id"],
                application_id=bundle.pinned["application_id"],
                application_instance_id=bundle.pinned["application_instance_id"],
                workflow_id=workflow_id,
                statework_memberships=authorized_memberships)
            if authorized_memberships:
                shared_branch_path = resolve_runtime.runtime_state_dir() / "shared-branches.sqlite3"
                session.shared_branch_ledger = SharedBranchLedger(shared_branch_path)
    if (runtime_mode == "standalone_enforcing" and bundle.stateworks
            and state_authority is None):
        raise ValueError(
            "standalone_enforcing with StateWorks requires an explicitly injected StateAuthority")
    if (runtime_mode != "combined_enforcing"
            and callable(getattr(state_authority, "bind_scope", None))):
        workflow_id = getattr(state_authority, "workflow_id", None)
        if not workflow_id:
            workflow_id = (
                f"private:{bundle.pinned.get('namespace_id', 'default')}:"
                f"{bundle.pinned.get('application_instance_id', 'unknown')}:"
                f"{session.agent_instance_id}")
        state_authority = state_authority.bind_scope(
            namespace_id=bundle.pinned.get("namespace_id", "default"),
            application_id=bundle.pinned.get("application_id", "cognitiveframeworks-runtime"),
            application_instance_id=bundle.pinned.get(
                "application_instance_id", "cognitiveframeworks-runtime"),
            workflow_id=workflow_id,
            statework_memberships={str(item.get("statework_id")): item
                                   for item in session.shared_workflow_memberships})
    if runtime_mode == "standalone_advisory" and state_authority is not None:
        raise ValueError("standalone_advisory cannot combine with shared StateWork authority")
    session.state_authority = state_authority
    session.policy_revocation_authority = policy_revocation_authority
    session.membership_authority = membership_authority
    session.operator_read_model = operator_read_model
    session.runtime_mode = runtime_mode
    if runtime_mode == "combined_enforcing" and not session._authoritative_state_ready():
        raise ValueError("shared StateWork authority could not resolve initial state")
    session._segment_status = {
        item.segment_id: (SegmentStatus.ACTIVE if index == 0 else SegmentStatus.PENDING)
        for index, item in enumerate(session.execution_segments)
    }
    journal_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(journal_root, 0o700)
    identity_digest = _journal_identity_prefix(bundle)
    session.journal_path = journal_root / f"session-{identity_digest}-{safe_task}-{session_nonce}.ndjson"
    session.journal_db_path = journal_root / f"session-{identity_digest}-{safe_task}-{session_nonce}.sqlite3"
    if not resume:
        if session.journal_path.exists() or session.journal_db_path.exists():
            raise FileExistsError(
                f"session journal already exists; use resume_task for {session_nonce!r}")
        session._journal("session_started", segment_id=session.current_segment.segment_id,
                         segment_index=session.current_segment_index,
                         authority_revision=session.authority_revision,
                         segment_status={key: value.value for key, value in session.segment_status.items()},
                         policy_epoch_record=(policy_epoch_record.to_dict()
                                              if policy_epoch_record else None))
    session.active_statework_ids = ({session.current_segment.statework_id}
                                    if session.current_segment.statework_id else set())
    session.lifecycle = LifecycleController(bundle)
    session.lifecycle.on_task_start({
        "task_complexity": (bundle.pinned.get("goal_contract") or {}).get(
            "task_complexity", "standard" if bundle.profile != "quick" else "trivial")})
    session.active_lifecycle = set(session.lifecycle.active)
    session.lifecycle_counts = dict(session.lifecycle.counts)
    if telemetry_sink is not None:
        session._emit = telemetry_sink
    elif not telemetry_disabled:
        digest = hashlib.sha256(
            session.execution_identity.storage_key().encode("utf-8")).hexdigest()[:24]
        store = (resolve_runtime.runtime_state_dir() / "telemetry" /
                 f"{digest}.ndjson")
        session._emit = HostTelemetry(
            bundle.task_id, store,
            expected_schema_hash=bundle.policy.behavior_event_schema_hash,
            expected_schema_version=bundle.policy.behavior_event_schema_version,
            namespace_id=bundle.pinned.get("namespace_id", "default"),
            application_id=bundle.pinned.get("application_id", "unknown-application"),
            application_version=bundle.pinned.get("application_version"),
            application_instance_id=bundle.pinned.get("application_instance_id"),
            provider_id=bundle.pinned.get("provider_id"),
            model_id=bundle.pinned.get("model_id", bundle.pinned.get("model")),
            model_revision=bundle.pinned.get("model_revision"),
            model_capability_hash=bundle.pinned.get("model_capability_hash"),
            harness_id=bundle.pinned.get("harness"),
            harness_version=bundle.pinned.get("harness_version"),
            agent_id=bundle.pinned.get("agent_id", "agent"),
            agent_instance_id=session.agent_instance_id,
            session_id=session.session_id, attempt_id=session.attempt_id,
            segment_id=session.current_segment.segment_id,
            interaction_id=session.interaction_id,
            parent_session_id=session.parent_session_id,
            delegator_agent_id=session.delegator_agent_id,
            delegate_agent_id=session.delegate_agent_id,
            delegation_id=session.delegation_id, role=session.role).emit
    session.tool_hooks.extend(tool_hooks or [])
    session.final_hooks.extend(final_hooks or [])
    if session._emit is not None and not resume:
        session._emit_runtime({
            "category": "decision", "event_type": "policy_composed",
            "subject": bundle.policy.subject_ref,
            "payload": {"summary": "host resolver froze task routing",
                        "result_class": "validated",
                        "policy_hash": bundle.pinned.get("policy_hash"),
                        "routing_profile_hash": bundle.policy.routing_profile_hash,
                        "intervention_assigned": ((bundle.guard_pack.get("routing_experiment") or {}).get("routing_cohort") == "treatment"),
                        "intervention_executed": False,
                        "effective_policy_hash": bundle.pinned.get("policy_hash")}})
        for stage in bundle.kernel:
            session._record_route_decision(
                session._host_capability, route=stage, route_type="stage",
                selection_reason="static_task_match")
        for item in bundle.stateworks:
            statework_id = item.get("id")
            session._record_route_decision(
                session._host_capability, route=str(statework_id), route_type="statework",
                selection_reason="state_requirement", event_type="statework_selected")
            flow = item.get("selected_flow")
            if flow:
                session._record_route_decision(
                    session._host_capability, route=Path(str(flow[0])).parent.name,
                    route_type="flow", selection_reason="static_task_match",
                    event_type="flow_selected")
            for specialist in item.get("selected_specialists", []):
                specialist_path = specialist[0] if isinstance(specialist, (list, tuple)) else specialist.get("path")
                session._record_route_decision(
                    session._host_capability, route=str(specialist_path),
                    route_type="specialist", selection_reason="static_task_match",
                    event_type="specialist_selected")
        for key in bundle.guard_pack.get("assigned_guard_keys", []):
            session._record_route_decision(
                session._host_capability, route=key, route_type="guard",
                selection_reason="static_task_match", event_type="guard_selected")
        for key in bundle.guard_pack.get("suppressed_guard_keys", []):
            session._record_route_decision(
                session._host_capability, route=key, route_type="guard",
                selection_reason="behavioral_profile_suppression", event_type="route_suppressed")
        for adjustment in bundle.guard_pack.get("routing_adjustments", []):
            route = adjustment.get("route")
            route_type = adjustment.get("route_type")
            if not route or not route_type:
                continue
            if adjustment.get("disposition") == "suppress":
                session._record_route_decision(
                    session._host_capability, route=str(route), route_type=str(route_type),
                    selection_reason="behavioral_profile_suppression",
                    event_type="route_suppressed")
            elif adjustment.get("disposition") in {"prefer", "activate"}:
                session._record_route_decision(
                    session._host_capability, route=str(route), route_type=str(route_type),
                    selection_reason="behavioral_profile_preference",
                    event_type="route_preferred")
    if telemetry_exporter is not None:
        object.__setattr__(session, "_telemetry_exporter", telemetry_exporter)
        telemetry_exporter.flush()
    if resume:
        _restore_session(session)
    resolve_runtime.write_snapshot(bundle)
    resolve_runtime.write_task_manifest(
        bundle, session_id=session.session_id, attempt_id=session.attempt_id,
        interaction_id=session.interaction_id, parent_session_id=session.parent_session_id,
        delegator_agent_id=session.delegator_agent_id,
        delegate_agent_id=session.delegate_agent_id, delegation_id=session.delegation_id,
        role=session.role)
    object.__setattr__(session, "_security_frozen", True)
    return session

def resume_task(bundle: resolve_runtime.RuntimeBundle, session_nonce: str, **kwargs: Any) -> RuntimeSession:
    """Resume a task only from its durable, policy-bound authority journal."""
    if not session_nonce:
        raise ValueError("resume requires the original session nonce")
    return start(bundle, session_nonce=session_nonce, resume=True, **kwargs)
