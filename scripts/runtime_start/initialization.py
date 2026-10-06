"""Initialize session journals, lifecycle, telemetry, and persisted artifacts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from runtime_contract.stateworks import SegmentStatus
from runtime_contract.telemetry import DurableTelemetryExporter
from runtime_session.journal import _journal_identity_prefix, _restore_session
from runtime_session.lifecycle import GateDecision, LifecycleController
from runtime_session.resolver_bridge import resolve_runtime
from runtime_session.session import RuntimeSession
from runtime_session.state import TelemetrySink
from runtime_session.standalone_route_policy import PolicyEpochRecord
from runtime_session.telemetry import HostTelemetry
from runtime_start.identity import StartIdentity


def initialize_runtime_session(
        session: RuntimeSession, bundle: Any, *, identity: StartIdentity,
        telemetry_sink: TelemetrySink | None,
        telemetry_exporter: DurableTelemetryExporter | None,
        telemetry_disabled: bool,
        tool_hooks: Sequence[Callable[[dict[str, Any]], GateDecision | None]] | None,
        final_hooks: Sequence[Callable[[dict[str, Any]], None]] | None,
        policy_epoch_record: PolicyEpochRecord | None,
        resume: bool) -> RuntimeSession:
    """Finish process-local lifecycle and durable startup initialization."""
    for index, item in enumerate(session.lifecycle_workflow.execution_segments):
        session.lifecycle_workflow.set_segment_status(
            item.segment_id,
            SegmentStatus.ACTIVE if index == 0 else SegmentStatus.PENDING)
    identity.journal_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(identity.journal_root, 0o700)
    identity_digest = _journal_identity_prefix(bundle)
    journal_path = identity.journal_root / (
        f"session-{identity_digest}-{identity.safe_task}-"
        f"{identity.session_nonce}.ndjson")
    journal_db_path = identity.journal_root / (
        f"session-{identity_digest}-{identity.safe_task}-"
        f"{identity.session_nonce}.sqlite3")
    session.telemetry_state.bind_journal(journal_path, journal_db_path)
    if not resume:
        if session.telemetry_state.journal_path.exists() or session.telemetry_state.journal_db_path.exists():
            raise FileExistsError(
                f"session journal already exists; use resume_task for "
                f"{identity.session_nonce!r}")
        session._journal(
            "session_started", segment_id=session.lifecycle_workflow.current_segment.segment_id,
            segment_index=session.lifecycle_workflow.current_segment_index,
            authority_revision=session.identity_authority.revisions.authority_revision,
            segment_status={key: value.value
                            for key, value in session.lifecycle_workflow.segment_status.items()},
            policy_epoch_record=(policy_epoch_record.to_dict()
                                 if policy_epoch_record else None))

    session.lifecycle_workflow.active_statework_ids = (
        {session.lifecycle_workflow.current_segment.statework_id}
        if session.lifecycle_workflow.current_segment.statework_id else set())
    session.lifecycle_workflow.bind_lifecycle(LifecycleController(bundle))
    session.lifecycle_workflow.lifecycle.on_task_start({
        "task_complexity": (bundle.pinned.get("goal_contract") or {}).get(
            "task_complexity", "standard" if bundle.profile != "quick" else "trivial")})
    session.lifecycle_workflow.active_lifecycle = set(session.lifecycle_workflow.lifecycle.active)
    session.lifecycle_workflow.lifecycle_counts = dict(session.lifecycle_workflow.lifecycle.counts)

    if telemetry_sink is not None:
        session.telemetry_state.bind_emitter(telemetry_sink)
    elif not telemetry_disabled:
        digest = hashlib.sha256(
            session.identity_authority.identity.execution_identity.storage_key().encode("utf-8")).hexdigest()[:24]
        store = resolve_runtime.runtime_state_dir() / "telemetry" / f"{digest}.ndjson"
        session.telemetry_state.bind_emitter(HostTelemetry(
            bundle.task_id, store,
            expected_schema_hash=bundle.policy.runtime_event_schema_hash,
            expected_schema_version=bundle.policy.runtime_event_schema_version,
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
            agent_instance_id=session.identity_authority.identity.agent_instance_id,
            session_id=session.identity_authority.identity.session_id, attempt_id=session.identity_authority.identity.attempt_id,
            segment_id=session.lifecycle_workflow.current_segment.segment_id,
            interaction_id=session.identity_authority.identity.interaction_id,
            parent_session_id=session.identity_authority.identity.parent_session_id,
            delegator_agent_id=session.identity_authority.identity.delegator_agent_id,
            delegate_agent_id=session.identity_authority.identity.delegate_agent_id,
            delegation_id=session.identity_authority.identity.delegation_id, role=session.identity_authority.identity.role).emit)

    session.tool_hooks.extend(tool_hooks or ())
    session.final_hooks.extend(final_hooks or ())
    if session.telemetry_state.emit is not None and not resume:
        _record_initial_route_decisions(session, bundle)
    if telemetry_exporter is not None:
        session.telemetry_state.bind_exporter(telemetry_exporter)
        telemetry_exporter.flush()
    if resume:
        _restore_session(session)
    resolve_runtime.write_snapshot(bundle)
    resolve_runtime.write_task_manifest(
        bundle, session_id=session.identity_authority.identity.session_id, attempt_id=session.identity_authority.identity.attempt_id,
        interaction_id=session.identity_authority.identity.interaction_id,
        parent_session_id=session.identity_authority.identity.parent_session_id,
        delegator_agent_id=session.identity_authority.identity.delegator_agent_id,
        delegate_agent_id=session.identity_authority.identity.delegate_agent_id,
        delegation_id=session.identity_authority.identity.delegation_id, role=session.identity_authority.identity.role)
    session.seal()
    return session


def _record_initial_route_decisions(session: RuntimeSession, bundle: Any) -> None:
    """Record the host resolver's frozen route choices once at task start."""
    session._emit_runtime({
        "category": "decision", "event_type": "policy_composed",
        "subject": bundle.policy.subject_ref,
        "payload": {
            "summary": "host resolver froze task routing",
            "result_class": "validated",
            "policy_hash": bundle.pinned.get("policy_hash"),
            "routing_profile_hash": bundle.policy.routing_profile_hash,
            "intervention_assigned": (
                (bundle.guard_pack.get("routing_experiment") or {}).get(
                    "routing_cohort") == "treatment"),
            "intervention_executed": False,
            "effective_policy_hash": bundle.pinned.get("policy_hash"),
        },
    })
    for stage in bundle.kernel:
        session._record_route_decision(
            session.identity_authority._host_capability, route=stage, route_type="stage",
            selection_reason="static_task_match")
    for item in bundle.stateworks:
        statework_id = item.get("id")
        session._record_route_decision(
            session.identity_authority._host_capability, route=str(statework_id),
            route_type="statework", selection_reason="state_requirement",
            event_type="statework_selected")
        flow = item.get("selected_flow")
        if flow:
            session._record_route_decision(
                session.identity_authority._host_capability, route=Path(str(flow[0])).parent.name,
                route_type="flow", selection_reason="static_task_match",
                event_type="flow_selected")
        for specialist in item.get("selected_specialists", []):
            specialist_path = (
                specialist[0] if isinstance(specialist, (list, tuple))
                else specialist.get("path"))
            session._record_route_decision(
                session.identity_authority._host_capability, route=str(specialist_path),
                route_type="specialist", selection_reason="static_task_match",
                event_type="specialist_selected")
    for key in bundle.guard_pack.get("assigned_guard_keys", []):
        session._record_route_decision(
            session.identity_authority._host_capability, route=key, route_type="guard",
            selection_reason="static_task_match", event_type="guard_selected")
    for key in bundle.guard_pack.get("suppressed_guard_keys", []):
        session._record_route_decision(
            session.identity_authority._host_capability, route=key, route_type="guard",
            selection_reason="behavioral_profile_suppression",
            event_type="route_suppressed")
    for adjustment in bundle.guard_pack.get("routing_adjustments", []):
        route = adjustment.get("route")
        route_type = adjustment.get("route_type")
        if not route or not route_type:
            continue
        disposition = adjustment.get("disposition")
        if disposition == "suppress":
            session._record_route_decision(
                session.identity_authority._host_capability, route=str(route),
                route_type=str(route_type),
                selection_reason="behavioral_profile_suppression",
                event_type="route_suppressed")
        elif disposition in {"prefer", "activate"}:
            session._record_route_decision(
                session.identity_authority._host_capability, route=str(route),
                route_type=str(route_type),
                selection_reason="behavioral_profile_preference",
                event_type="route_preferred")
