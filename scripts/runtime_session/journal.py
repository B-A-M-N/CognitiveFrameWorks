"""Durable CFW session journal identity, loading, and recovery."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, List, Mapping

from runtime_contract.convergence import AttemptDescriptor
from runtime_contract.evidence import CompletionPermit, EvidenceRecord, StateSegment
from runtime_contract.metacognition import MetaDecision, SemanticSignals
from runtime_contract.stateworks import SegmentStatus
from runtime_contract.system_one import AttemptSemanticDescriptor, SystemOneIdentity
from runtime_session.behavior_authority import StandaloneBehaviorAuthority
from runtime_session.resolver_bridge import resolve_runtime
from runtime_session.session import RuntimeSession
from runtime_session.standalone_route_policy import PolicyEpochRecord

def _journal_identity_prefix(bundle: resolve_runtime.RuntimeBundle) -> str:
    values = (
        bundle.pinned.get("namespace_id", "default"),
        bundle.pinned.get("application_instance_id", "unknown-application"),
        bundle.pinned.get("agent_instance_id", bundle.pinned.get("agent_id", "agent")),
        bundle.task_id, bundle.pinned.get("attempt_id", "attempt-1"),
        bundle.pinned.get("interaction_id") or "",
    )
    return hashlib.sha256("|".join(str(value) for value in values).encode("utf-8")).hexdigest()[:32]

def _journal_records(session: RuntimeSession) -> List[Dict[str, Any]]:
    """Read the durable authority log, preferring the SQLite WAL copy."""
    records: List[Dict[str, Any]] = []
    if session.telemetry_state.journal_db_path is not None and session.telemetry_state.journal_db_path.exists():
        with sqlite3.connect(session.telemetry_state.journal_db_path) as database:
            rows = database.execute(
                "SELECT event_json FROM authority_events ORDER BY seq").fetchall()
        for (encoded,) in rows:
            records.append(json.loads(encoded))
    elif session.telemetry_state.journal_path is not None and session.telemetry_state.journal_path.exists():
        records = [json.loads(line) for line in
                   session.telemetry_state.journal_path.read_text(encoding="utf-8").splitlines() if line]
    if not records:
        raise FileNotFoundError("no durable authority journal exists for this session")
    return records

def _restore_session(session: RuntimeSession) -> None:
    """Replay only host-authenticated authority facts into a new session."""
    records = _journal_records(session)
    for record in records:
        if (record.get("task_id") != session.bundle.task_id
                or record.get("session_nonce") != session.identity_authority.identity.session_nonce
                or record.get("session_id") != session.identity_authority.identity.session_id
                or record.get("agent_instance_id") != session.identity_authority.identity.agent_instance_id
                or record.get("policy_hash") != session.bundle.pinned["policy_hash"]):
            raise PermissionError("session journal is not bound to this task/policy/session")
        event_type = record.get("event_type")
        if event_type == "session_started":
            session.lifecycle_workflow.current_segment_index = int(record.get("segment_index", 0))
            session.identity_authority.revisions.observe_authority(
                int(record.get("authority_revision", 0) or 0))
            epoch_data = record.get("policy_epoch_record")
            restored_epoch = (PolicyEpochRecord.from_mapping(epoch_data)
                              if isinstance(epoch_data, Mapping) else None)
            if restored_epoch is not None:
                if session.behavior.is_external:
                    raise PermissionError(
                        "external behavior sessions cannot restore standalone route epochs")
                if restored_epoch.child_policy_hash != session.bundle.pinned.get("policy_hash"):
                    raise PermissionError("child policy epoch journal differs from resumed bundle")
                route_policy = session.ensure_standalone_behavior_authority().route_policy
                if (route_policy.policy_epoch_record is not None
                        and route_policy.policy_epoch_record != restored_epoch):
                    raise PermissionError("supplied policy epoch differs from resumed journal")
                route_policy.restore_policy_epoch(restored_epoch)
            elif (session.behavior.standalone_route_policy is not None
                    and session.behavior.standalone_route_policy.policy_epoch_record is not None):
                raise PermissionError("resumed policy epoch is absent from the durable journal")
            statuses = record.get("segment_status") or {}
            for segment_id, status in statuses.items():
                session.lifecycle_workflow.set_segment_status(
                    segment_id, SegmentStatus(status))
        elif event_type == "behavior_signal_recorded":
            cursor = int(record.get("delivery_cursor", 0))
            envelope = record.get("envelope")
            if cursor != session.behavior.signal_cursor + 1 or not isinstance(
                    envelope, Mapping):
                raise PermissionError("behavior signal journal sequence is malformed")
            signal = dict(envelope)
            signal_id = str(signal.get("signal_id") or "")
            if not signal_id or signal_id in session.behavior.signal_seen:
                raise PermissionError("behavior signal journal identity is malformed")
            session.behavior.signal_seen[signal_id] = signal
            session.behavior.signals.append(signal)
            session.behavior.signal_journal.append((cursor, signal))
            session.behavior.signal_cursor = cursor
        elif event_type == "behavior_signal_acknowledged":
            cursor = int(record.get("through_cursor", -1))
            if not session.behavior.signal_ack_cursor <= cursor <= session.behavior.signal_cursor:
                raise PermissionError("behavior signal acknowledgement journal is malformed")
            session.behavior.signal_ack_cursor = cursor
            session.behavior.signal_journal = [
                (item_cursor, envelope)
                for item_cursor, envelope in session.behavior.signal_journal
                if item_cursor > cursor
            ]
        elif event_type == "segment_activated":
            session.lifecycle_workflow.current_segment_index = int(record["segment_index"])
            session.lifecycle_workflow.set_segment_status(
                record["segment_id"], SegmentStatus.ACTIVE)
        elif event_type == "policy_revocation_observed":
            session.identity_authority.revisions.observe_policy_revocation(
                int(record.get("policy_revocation_revision", 0) or 0),
                str(record.get("revocation_mode") or
                    session.identity_authority.revisions.revocation_mode))
        elif event_type == "convergence_recorded":
            if session.attempts.controller is not None:
                session.attempts.controller.restore(record.get("convergence") or {})
        elif event_type == "route_decision_recorded":
            decision_id = str(record.get("decision_id") or record.get("route_decision_id") or "")
            if decision_id:
                session.actions.route_decisions[decision_id] = dict(record.get("decision") or {})
        elif event_type == "route_outcome_recorded":
            event_id = str(record.get("event_id") or "")
            if event_id:
                session.actions.route_outcomes[event_id] = dict(record.get("outcome") or {})
        elif event_type == "system_one_sequence":
            session.behavior.system_one_sequence = max(
                session.behavior.system_one_sequence, int(record.get("sequence", 0) or 0))
        elif event_type == "system_one_projection_changed":
            session.behavior.metacognitive.restore(record.get("snapshot") or {})
        elif event_type == "semantic_signal_store_changed":
            if (int(record.get("metacognitive_revision", -1)) >= 0
                    and not session.behavior.is_external):
                records = tuple(record.get("records") or ())
                if session.behavior.authority is None:
                    session.behavior.deferred_semantic_signal_records = records
                else:
                    session.behavior.semantic_signal_store.restore(records)
        elif event_type == "system_one_decision_recorded":
            decision = dict(record.get("decision") or {})
            decision_id = str(decision.get("decision_id") or "")
            if decision_id:
                session.behavior.system_one_decisions[decision_id] = decision
                session.behavior.system_one_sequence = max(
                    session.behavior.system_one_sequence, int(decision.get("sequence", 0) or 0))
        elif event_type == "system_one_outcome_recorded":
            session.behavior.system_one_outcomes.add(
                f"{record.get('decision_id') or ''}:{record.get('label') or ''}")
        elif event_type == "evidence_attested":
            evidence_id = str(record["evidence_id"])
            session.evidence.ledger._records[evidence_id] = EvidenceRecord(
                evidence_id=evidence_id, task_id=session.bundle.task_id,
                kind=str(record["evidence_type"]),
                subject_ref=record.get("subject_ref"),
                observed_at=str(record.get("observed_at") or record["timestamp"]),
                invalidated=bool(record.get("invalidated", False)),
                event_id=record.get("source_event_id"),
                issuer=record.get("issuer"),
                source_event_id=record.get("source_event_id"),
                invocation_id=record.get("invocation_id"),
                content_digest=record.get("content_digest"),
                claim_digest=record.get("claim_digest"),
                covered_boundary_ids=tuple(
                    record.get("covered_boundary_ids") or ()))
        elif event_type == "evidence_invalidated":
            evidence_id = str(record["evidence_id"])
            session.evidence.ledger.invalidate(evidence_id, writer_token=session.evidence._writer_token)
            session.identity_authority.revisions.observe_authority(
                int(record.get("authority_revision",
                               session.identity_authority.revisions.authority_revision) or 0))
        elif event_type == "tool_invocation_completed":
            session.actions.host_invocations[str(record["invocation_id"])] = {
                "tool_type": record.get("tool_type", ""),
                "result_class": record.get("result_class", ""),
                "subject_ref": record.get("subject_ref"),
                "evidence_id": record.get("evidence_id"),
                "action_request_id": record.get("action_request_id"),
                # Raw tool results are deliberately not persisted.  A resumed
                # completion path must receive a fresh host validator input.
                "actual_result": {},
            }
        elif event_type == "confirmation_consumed":
            session.actions.consume_grant(str(record["grant_id"]))
        elif event_type == "contradiction_recorded":
            contradiction_id = str(record["contradiction_id"])
            session.evidence.contradictions[contradiction_id] = {
                "id": contradiction_id, "subject": record.get("subject_ref"),
                "invalidates": record.get("invalidates"), "resolved": False,
                "timestamp": record.get("timestamp"),
            }
            session.identity_authority.revisions.observe_authority(
                int(record.get("authority_revision",
                               session.identity_authority.revisions.authority_revision) or 0))
        elif event_type == "completion_permit_revoked":
            session.evidence.revoke_completion_permit()
        elif event_type == "contradiction_resolved":
            contradiction = session.evidence.contradictions.get(str(record["contradiction_id"]))
            if contradiction is not None:
                contradiction["resolved"] = True
        elif event_type == "state_transition_allowed":
            key = (str(record["statework_id"]), str(record["subject_ref"]))
            session.lifecycle_workflow.statework_states[key] = str(record["output_state"])
            session.lifecycle_workflow.statework_entry_times[key] = str(record.get("recorded_at") or record["timestamp"])
            session.evidence.state_segments.append(StateSegment(
                segment_id=str(record.get("segment_id") or f"restored-{len(session.evidence.state_segments)}"),
                task_id=session.bundle.task_id, subject_ref=key[1],
                input_state=str(record["input_state"]), output_state=str(record["output_state"]),
                evidence_refs=tuple(record.get("evidence_refs") or ()),
                recorded_at=session.lifecycle_workflow.statework_entry_times[key], statework_id=key[0]))
            transition = record.get("transition") or {}
            csw_progress_class = str(record.get("csw_progress_class")
                                      or transition.get("progress_class")
                                      or "LATERAL")
            progress_class = "WORKFLOW_PROGRESS"
            session.behavior.metacognitive.record_workflow_progress(
                statework_id=key[0], subject_ref=key[1],
                engine_kind=str(transition.get("engine_kind") or "state_machine"),
                workflow_revision=int(transition.get("revision") or 0),
                step_id=str(transition.get("step_id") or transition.get("edge_id") or "transition"),
                progress_class=csw_progress_class,
                completed=transition.get("completed") is True,
                work_class=str(transition.get("work_class") or "workflow"),
                domain_effect=str(transition.get("domain_effect") or "workflow"),
                current_position=str(record["output_state"]),
                previous_position=str(record["input_state"]),
                evidence_refs=tuple(record.get("evidence_refs") or ()))
            if session.attempts.controller is not None:
                progress_digest = hashlib.sha256(json.dumps({
                    "statework_id": key[0], "subject_ref": key[1],
                    "input_state": record.get("input_state"),
                    "output_state": record.get("output_state"),
                    "edge_id": transition.get("edge_id"),
                }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                evidence_refs = list(record.get("evidence_refs") or ())
                session.attempts.controller.record_progress(
                    cause_id="primary-objective", progress_digest=progress_digest,
                    progress_class="WORKFLOW_PROGRESS",
                    evidence_ref=evidence_refs[-1] if evidence_refs else None)
        elif event_type == "workflow_step_allowed":
            session.behavior.metacognitive.record_workflow_progress(
                statework_id=str(record["statework_id"]),
                subject_ref=str(record["subject_ref"]),
                engine_kind=str(record["engine_kind"]),
                step_id=str(record["step_id"]),
                workflow_revision=int(record["revision"]),
                progress_class=str(record["progress_class"]),
                completed=bool(record["completed"]),
                work_class=str(record.get("work_class") or "workflow"),
                domain_effect=str(record.get("domain_effect") or "workflow"),
                evidence_refs=tuple(record.get("evidence_refs") or ()))
            if session.attempts.controller is not None:
                digest = hashlib.sha256(json.dumps({
                    "statework_id": record.get("statework_id"),
                    "subject_ref": record.get("subject_ref"),
                    "engine_kind": record.get("engine_kind"),
                    "step_id": record.get("step_id"),
                    "revision": record.get("revision"),
                }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                refs = list(record.get("evidence_refs") or ())
                session.attempts.controller.record_progress(
                    cause_id="primary-objective", progress_digest=digest,
                    progress_class="WORKFLOW_PROGRESS",
                    evidence_ref=refs[-1] if refs else None)
        elif event_type == "handoff_attested":
            session.lifecycle_workflow.handoffs[str(record["packet_id"])] = {
                "packet_id": record["packet_id"], "producer": record.get("producer"),
                "subject_ref": record.get("subject_ref"),
            }
            segment_id = record.get("segment_id")
            if segment_id in session.lifecycle_workflow.statuses:
                session.lifecycle_workflow.set_segment_status(
                    segment_id, SegmentStatus.COMPLETED)
        elif event_type == "completion_permit_issued":
            session.evidence.issue_completion_permit(CompletionPermit(
                task_id=session.bundle.task_id,
                policy_hash=session.bundle.pinned["policy_hash"],
                subject_ref=record.get("subject_ref"),
                claim_digest=str(record.get("claim_digest") or ""),
                evidence_refs=tuple(record.get("evidence_refs") or ()),
                issued_at=str(record.get("issued_at") or record["timestamp"]),
                session_nonce=session.identity_authority.identity.session_nonce,
                authority_revision=int(record.get("authority_revision", 0) or 0),
                boundary_type=str(record.get("boundary_type") or "completion"),
                boundary_target=str(record.get("boundary_target") or "task"),
                expected_validator=str(record.get("expected_validator") or
                                      "completion-boundary-v1"),
                goal_contract_hash=str(record.get("goal_contract_hash") or ""),
                criterion_ids=tuple(record.get("criterion_ids") or ()),
                criterion_evidence_refs=tuple(
                    (str(key), tuple(refs))
                    for key, refs in record.get("criterion_evidence_refs") or ())))
        elif event_type == "attempt_descriptor_recorded":
            descriptor = AttemptDescriptor(**dict(record.get("descriptor") or {}))
            session.attempts.descriptors[descriptor.attempt_ref] = descriptor
            semantic_data = record.get("semantic_descriptor")
            if semantic_data:
                semantic_data = dict(semantic_data)
                semantic_data["affected_criterion_ids"] = tuple(
                    semantic_data.get("affected_criterion_ids") or ())
                session.attempts.semantics[descriptor.attempt_ref] = AttemptSemanticDescriptor(
                    **semantic_data)
        elif event_type == "attempt_failure_recorded":
            session.attempts.failures.add(str(record.get("attempt_ref") or ""))
    session.behavior.metacognitive.ensure_criteria(
        session.bundle.pinned.get("goal_contract") or {})
    current = session.lifecycle_workflow.current_segment
    session.lifecycle_workflow.active_statework_ids = ({current.statework_id}
                                    if current.statework_id else set())
    if session.telemetry_state.journal_path is not None:
        os.chmod(session.telemetry_state.journal_path, 0o600)
    if session.telemetry_state.journal_db_path is not None and session.telemetry_state.journal_db_path.exists():
        os.chmod(session.telemetry_state.journal_db_path, 0o600)
    # A resumed model-visible behavior decision may have been based on stale
    # state. Keep the host reconstruction, clear transient influence, then
    # send a complete frame on the next eligible query.
    if isinstance(session.behavior.authority, StandaloneBehaviorAuthority):
        session.behavior.set_semantic_signals(SemanticSignals())
        session.behavior.set_metacognitive_decision(
            MetaDecision("DELIVER", "session restored"))
    session.behavior.semantic_signals_revision = -1
    session.actions.clear_pending()
    session.attempts.clear_pending_retries()
    if session.behavior.system_one_judge is not None:
        identity = SystemOneIdentity(
            namespace_id=session.identity_authority.identity.execution_identity.namespace_id,
            application_id=str(session.bundle.pinned.get("application_id") or "unknown-application"),
            application_instance_id=session.identity_authority.identity.execution_identity.application_instance_id,
            agent_id=str(session.bundle.pinned.get("agent_id") or "agent"),
            agent_instance_id=session.identity_authority.identity.execution_identity.agent_instance_id,
            session_id=session.identity_authority.identity.execution_identity.session_id,
            task_id=session.identity_authority.identity.execution_identity.task_id,
            attempt_id=session.identity_authority.identity.execution_identity.attempt_id,
            interaction_id=session.identity_authority.identity.execution_identity.interaction_id)
        reset = getattr(session.behavior.system_one_judge, "reset_identity", None)
        if callable(reset):
            reset(identity)
    session._evaluate_metacognition(semantic=SemanticSignals())
    session._journal("system_one_resume_state_invalidated",
                     projector_restored=True, metacognitive_control_recomputed=True,
                     provider_resync_required=True)
