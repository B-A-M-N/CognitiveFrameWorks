"""Completion readiness, permits, and final response methods for CFW."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from runtime_contract.evidence import (
    CompletionClaim, CompletionPermit, TerminationPermit, completion_claim_digest,
)
from runtime_contract.handlers import handler_for_enforcement
from runtime_contract.stateworks import SegmentStatus
from runtime_session.host_authority import _HostCapability
from runtime_session.lifecycle import GateDecision
from runtime_session.standalone_behavior import _sispis_calibration


@dataclass(frozen=True)
class BehaviorSignalBatch:
    after_cursor: int
    next_cursor: int
    signals: tuple[Mapping[str, Any], ...]


class CompletionMethods:
    """Completion proof, termination boundary, and output finalization."""
    def request_termination(self, capability: _HostCapability, *,
                           disposition: str, reason: str, cause_id: str,
                           evidence_refs: Iterable[str] = (),
                           last_verified_state: Optional[str] = None,
                           unresolved_blockers: Iterable[str] = ()) -> TerminationPermit:
        """Terminate as blocked/failed/handoff without claiming success."""
        self._require_host(capability)
        if disposition not in {"BLOCKED", "FAILED", "HANDOFF"}:
            raise ValueError("termination disposition must be BLOCKED, FAILED or HANDOFF")
        if self.termination_disposition == "SUCCEEDED":
            raise PermissionError("a completed task cannot be re-terminated")
        resolved_refs: list[str] = []
        for ref in evidence_refs:
            record = self.ledger.get(str(ref))
            if record is None or record.invalidated:
                continue
            resolved_refs.append(str(ref))
        claim_digest = hashlib.sha256(json.dumps({
            "task_id": self.bundle.task_id, "policy_hash": self.bundle.pinned["policy_hash"],
            "subject_ref": self.bundle.policy.subject_ref, "disposition": disposition,
            "cause_id": cause_id, "reason": reason, "evidence_refs": sorted(resolved_refs),
            "last_verified_state": last_verified_state,
            "unresolved_blockers": sorted(str(item) for item in unresolved_blockers),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        permit = TerminationPermit(
            task_id=self.bundle.task_id, policy_hash=self.bundle.pinned["policy_hash"],
            subject_ref=self.bundle.policy.subject_ref, disposition=disposition,
            cause_id=cause_id, reason=reason, claim_digest=claim_digest,
            evidence_refs=tuple(sorted(resolved_refs)),
            issued_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            session_nonce=self.session_nonce, authority_revision=self.authority_revision,
            last_verified_state=last_verified_state,
            unresolved_blockers=tuple(sorted(str(item) for item in unresolved_blockers)))
        self.termination_disposition = disposition
        self.termination_permit = permit
        if self.convergence is not None:
            self.convergence.terminate(disposition=disposition, reason=reason)
            self._record_convergence(f"termination:{disposition}")
        self._journal("task_terminated", disposition=disposition, reason=reason,
                      cause_id=cause_id, claim_digest=claim_digest,
                      evidence_refs=list(permit.evidence_refs),
                      last_verified_state=last_verified_state,
                      unresolved_blockers=list(permit.unresolved_blockers))
        # behavior-event.schema.json forbids extra top-level fields, so causal
        # lineage travels in the bounded payload rather than a new column.
        event = {"category": "blocker", "event_type": "task_terminated",
                 "subject": self.bundle.policy.subject_ref,
                 "payload": {"summary": reason, "result_class": disposition,
                             # cause lineage travels in the bounded `reason`
                             # field; the payload schema is closed.
                             "reason": cause_id}}
        # A blocker may legitimately have no evidence refs; the schema requires
        # a non-empty list when the field is present, so omit it rather than
        # inventing a reference.
        if permit.evidence_refs:
            event["evidence_refs"] = list(permit.evidence_refs)
        self._emit_runtime(event)
        return permit
    def finish_task_terminated(self, output: Optional[Dict[str, Any]] = None,
                               *, permit: Optional[TerminationPermit] = None) -> Dict[str, Any]:
        """Finalize a blocked/failed/handoff task using its TerminationPermit."""
        permit = permit or self.termination_permit
        if permit is None or not isinstance(permit, TerminationPermit):
            raise PermissionError("termination requires a host-issued TerminationPermit")
        if permit.task_id != self.bundle.task_id or permit.policy_hash != self.bundle.pinned["policy_hash"]:
            raise PermissionError("termination permit is not bound to this task/policy")
        if permit.session_nonce != self.session_nonce or permit is not self.termination_permit:
            raise PermissionError("termination permit is not bound to this runtime session")
        if permit.disposition != self.termination_disposition:
            raise PermissionError("termination permit disposition disagrees with the runtime")
        if permit.authority_revision != self.authority_revision:
            raise PermissionError("termination permit predates a later authority revision")
        return self._render_output({
            **(dict(output) if output else {}),
            "status": permit.disposition.lower(),
            "reason": permit.reason,
            "cause_id": permit.cause_id,
            "completed": False,
            "last_verified_state": permit.last_verified_state,
            "unresolved_blockers": list(permit.unresolved_blockers),
        }, complete=True)
    def _criterion_evidence_kinds(self, kind: str) -> set[str]:
        defaults = {
            "static_visual": {"rendered_observation"},
            "visual_static": {"rendered_observation"},
            "responsive_layout": {"rendered_viewport_matrix"},
            "interaction": {"interaction_validation"},
            "terminal_lifecycle": {"pty_validation"},
            "legibility": {"rendered_observation"},
            "code_behavior": {"tool_result"},
            "test_suite": {"tool_result"},
        }
        canonical_kind = "static_visual" if kind == "visual_static" else kind
        required = set(defaults.get(canonical_kind, ()))
        for statework in self.bundle.stateworks:
            metadata = statework.get("flow_metadata") or {}
            completion = metadata.get("completion") or {}
            claim_classes = completion.get("claim_classes") or {}
            item = claim_classes.get(canonical_kind)
            if isinstance(item, Mapping):
                required.update(str(value) for value in item.get("evidence_kinds") or ())
        return required
    def _validate_criterion_proofs(self, criterion_ids: Iterable[str],
                                   criterion_evidence_refs: Iterable[tuple[str, Iterable[str]]],
                                   subject_ref: Optional[str]) -> None:
        goal = self.bundle.pinned.get("goal_contract") or {}
        criteria = {str(item.get("criterion_id")): item
                    for item in goal.get("acceptance_criteria", ())}
        proofs = {str(key): tuple(refs) for key, refs in criterion_evidence_refs}
        if set(proofs) - set(criteria):
            raise ValueError("criterion evidence references an unknown criterion")
        for criterion_id in criterion_ids:
            criterion = criteria.get(str(criterion_id))
            refs = proofs.get(str(criterion_id), ())
            if criterion is None or not refs:
                raise ValueError(f"criterion {criterion_id!r} lacks host-attested evidence")
            expected_kinds = self._criterion_evidence_kinds(str(criterion.get("kind") or ""))
            self.ledger.resolve(
                list(refs), subject_ref=subject_ref,
                allowed_kinds=expected_kinds or None)
    def record_completion_claim(self, subject: Optional[str], evidence_refs: List[str],
                                *, boundary_type: str = "completion",
                                boundary_target: str = "task",
                                expected_validator: str = "completion-boundary-v1",
                                claim: Optional[CompletionClaim] = None) -> GateDecision:
        for enforcement in self.bundle.guard_pack.get("structural_handlers", {}).values():
            if enforcement.get("mode") not in {"structural", "state_transition"}:
                continue
            handler = handler_for_enforcement(enforcement)
            if handler and "before_completion" in getattr(handler, "hooks", ()):
                result = handler.before_completion(self, claim, enforcement)
                if result == "require_reinspection":
                    self._emit_runtime({
                        "category": "blocker",
                        "event_type": "completion_blocked_by_claim",
                        "subject": subject,
                        "evidence_refs": evidence_refs,
                        "payload": {"summary": "completion requires an explicit claim binding",
                                    "result_class": "boundary evidence required"}})
                    return GateDecision.REQUIRE_REINSPECTION
        if claim is None:
            self._emit_runtime({"category": "blocker", "event_type": "completion_blocked_by_claim",
                       "subject": subject, "evidence_refs": evidence_refs,
                       "payload": {"summary": "completion requires an explicit claim binding",
                                   "result_class": "boundary evidence required"}})
            return GateDecision.REQUIRE_REINSPECTION
        goal_contract = self.bundle.pinned.get("goal_contract") or {}
        expected_goal_hash = str(self.bundle.pinned.get("goal_contract_hash") or "")
        required_criterion_ids = {
            str(item.get("criterion_id"))
            for item in goal_contract.get("acceptance_criteria", ())
            if item.get("required", True)}
        known_criterion_ids = {
            str(item.get("criterion_id"))
            for item in goal_contract.get("acceptance_criteria", ())}
        claim_criterion_ids = set(claim.criterion_ids)
        if not expected_goal_hash or not required_criterion_ids:
            self._emit_runtime({
                "category": "blocker", "event_type": "completion_blocked_by_goal_contract",
                "subject": subject,
                **({"evidence_refs": evidence_refs} if evidence_refs else {}),
                "payload": {"summary": "successful completion requires a pinned task goal contract",
                            "result_class": "task goal contract required"}})
            return GateDecision.REQUIRE_REINSPECTION
        if (claim.goal_contract_hash != expected_goal_hash
                or not required_criterion_ids.issubset(claim_criterion_ids)
                or not claim_criterion_ids.issubset(known_criterion_ids)):
            self._emit_runtime({
                "category": "blocker", "event_type": "completion_blocked_by_claim",
                "subject": subject, "evidence_refs": evidence_refs,
                "payload": {"summary": "completion claim does not cover the pinned task criteria",
                            "result_class": "task contract criteria required"}})
            return GateDecision.REQUIRE_REINSPECTION
        try:
            self._validate_criterion_proofs(
                claim_criterion_ids, claim.criterion_evidence_refs, subject)
        except ValueError as exc:
            self._emit_runtime({
                "category": "blocker", "event_type": "completion_blocked_by_criterion_evidence",
                "subject": subject,
                "payload": {"summary": str(exc)[:512],
                            "result_class": "criterion evidence required"}})
            return GateDecision.REQUIRE_REINSPECTION
        authoritative_subject = self.bundle.policy.subject_ref
        if (not subject or (authoritative_subject is not None
                            and subject != authoritative_subject)):
            self._emit_runtime({
                "category": "blocker", "event_type": "completion_subject_mismatch",
                "subject": subject, "evidence_refs": evidence_refs,
                "payload": {"summary": "completion requires the exact task subject",
                            "result_class": "blocked"}})
            return GateDecision.BLOCK
        if any(not record["resolved"] and (subject is None or record["subject"] == subject)
               for record in self.contradictions.values()):
            self._emit_runtime({"category": "blocker", "event_type": "completion_blocked_by_contradiction",
                       "subject": subject, "evidence_refs": evidence_refs,
                       "payload": {"summary": "relevant operator contradiction is unresolved",
                                   "result_class": "reinspection required"}})
            return GateDecision.REQUIRE_REINSPECTION
        try:
            resolved = self.ledger.resolve(
                list(evidence_refs), subject_ref=subject,
                allowed_kinds={"boundary_evidence", "completion_boundary"})
            boundary_records = [self.ledger.get(ref) for ref in evidence_refs]
            if any(record is None or not record.claim_digest or not record.invocation_id
                   for record in boundary_records):
                raise ValueError("completion evidence is not bound to a claim")
            claim_digest = claim.claim_digest if claim is not None else None
            if claim is not None:
                if (claim.task_id != self.bundle.task_id or claim.subject_ref != subject
                        or claim.boundary_type != boundary_type
                        or claim.boundary_target != boundary_target
                        or claim.expected_validator != expected_validator
                        or claim.goal_contract_hash != expected_goal_hash):
                    raise ValueError("completion claim identity does not match this task")
                if claim.invocation_id not in {
                        record.invocation_id for record in boundary_records}:
                    raise ValueError("completion claim invocation is not the attested invocation")
            for record in boundary_records:
                expected = completion_claim_digest(
                    self.bundle.task_id, subject, boundary_type, boundary_target,
                    expected_validator, record.invocation_id or "",
                    expected_goal_hash, tuple(claim.criterion_ids),
                    tuple(claim.criterion_evidence_refs))
                if claim_digest is None:
                    claim_digest = record.claim_digest
                if record.claim_digest != claim_digest or record.claim_digest != expected:
                    raise ValueError("completion evidence is bound to a different claim")
        except ValueError as exc:
            self._emit_runtime({"category": "blocker", "event_type": "completion_blocked_by_evidence",
                       "subject": subject, "evidence_refs": evidence_refs,
                       "payload": {"summary": str(exc)[:512], "result_class": "boundary evidence required"}})
            return GateDecision.REQUIRE_REINSPECTION
        self._emit_runtime({"category": "completion", "event_type": "completion_attempted",
                   "subject": subject, "evidence_refs": evidence_refs,
                   "payload": {"summary": "completion claim validated",
                               "evidence_reference": claim_digest or "",
                               "criterion_ids": sorted(claim_criterion_ids),
                               "completion_criterion_coverage": (
                                   len(required_criterion_ids & claim_criterion_ids)
                                   / max(1, len(required_criterion_ids)))}})
        object.__setattr__(self, "_completion_permit", CompletionPermit(
            task_id=self.bundle.task_id,
            policy_hash=self.bundle.pinned["policy_hash"],
            subject_ref=subject,
            claim_digest=claim_digest or "",
            evidence_refs=tuple(evidence_refs),
            issued_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            session_nonce=self.session_nonce,
            authority_revision=self.authority_revision,
            boundary_type=boundary_type, boundary_target=boundary_target,
            expected_validator=expected_validator,
            goal_contract_hash=expected_goal_hash,
            criterion_ids=tuple(sorted(claim_criterion_ids)),
            criterion_evidence_refs=tuple(claim.criterion_evidence_refs)))
        self._journal("completion_permit_issued", claim_digest=claim_digest,
                      evidence_refs=list(evidence_refs), subject_ref=subject,
                      issued_at=self._completion_permit.issued_at,
                      authority_revision=self.authority_revision,
                      boundary_type=boundary_type,
                      boundary_target=boundary_target,
                      expected_validator=expected_validator,
                      goal_contract_hash=expected_goal_hash,
                      criterion_ids=sorted(claim_criterion_ids),
                      criterion_evidence_refs=[
                          [key, list(refs)]
                          for key, refs in claim.criterion_evidence_refs])
        return GateDecision.ALLOW
    def emit_signal(self, envelope: Dict[str, Any]) -> None:
        """Validate, store, and emit a task-local canonical signal envelope."""
        calibration = _sispis_calibration()
        contracts = self.bundle.pinned.get("contracts", {})
        calibration._validate_envelope(
            envelope,
            signal_schema=contracts.get("signal_schema"),
            signal_registry=contracts.get("signal_registry"))
        signal_id = envelope["signal_id"]
        with self._behavior_signal_lock:
            prior = self.signal_seen.get(signal_id)
            if prior and prior != envelope:
                raise ValueError(f"conflicting signal envelope {signal_id!r}")
            if prior == envelope:
                return
            cursor = self._behavior_signal_cursor + 1
            recorded = deepcopy(envelope)
            # Persist the delivery journal before publishing it to in-memory
            # consumers. A restart can therefore replay an unacknowledged
            # signal without mutating the audit history.
            self._journal("behavior_signal_recorded", delivery_cursor=cursor,
                          envelope=recorded)
            self.signal_seen[signal_id] = recorded
            self.signals.append(deepcopy(recorded))
            self._behavior_signal_journal.append((cursor, recorded))
            self._behavior_signal_cursor = cursor
        self._emit_runtime({"category": "decision", "event_type": "signal_emitted",
                   "subject": envelope.get("subject_ref"),
                   "payload": {"summary": envelope["signal_type"][:512],
                               "result_class": envelope["required_action"][:64]}})

    def peek_behavior_signals(self, *, after_cursor: int | None = None,
                              maximum: int = 32) -> BehaviorSignalBatch:
        """Return a bounded batch of unacknowledged signals without consuming it."""
        if type(maximum) is not int or not 1 <= maximum <= 32:
            raise ValueError("behavior signal batch size must be between one and 32")
        with self._behavior_signal_lock:
            cursor = (self._behavior_signal_ack_cursor if after_cursor is None
                      else after_cursor)
            if type(cursor) is not int or cursor < self._behavior_signal_ack_cursor:
                raise ValueError("behavior signal cursor precedes the acknowledged cursor")
            if cursor > self._behavior_signal_cursor:
                raise ValueError("behavior signal cursor exceeds the journal")
            selected = [(item_cursor, envelope)
                        for item_cursor, envelope in self._behavior_signal_journal
                        if item_cursor > cursor][:maximum]
            next_cursor = selected[-1][0] if selected else cursor
            return BehaviorSignalBatch(
                after_cursor=cursor,
                next_cursor=next_cursor,
                signals=tuple(deepcopy(envelope) for _, envelope in selected),
            )

    def acknowledge_behavior_signals(self, *, through_cursor: int) -> None:
        """Durably acknowledge a committed signal batch; repeated ACKs are safe."""
        if type(through_cursor) is not int or through_cursor < 0:
            raise ValueError("behavior signal acknowledgement cursor is invalid")
        with self._behavior_signal_lock:
            if through_cursor <= self._behavior_signal_ack_cursor:
                return
            if through_cursor > self._behavior_signal_cursor:
                raise ValueError("cannot acknowledge behavior signals beyond the journal")
            self._journal("behavior_signal_acknowledged",
                          through_cursor=through_cursor)
            self._behavior_signal_ack_cursor = through_cursor
            self._behavior_signal_journal = [
                (cursor, envelope)
                for cursor, envelope in self._behavior_signal_journal
                if cursor > through_cursor
            ]
    def _render_output(self, output: Optional[Dict[str, Any]] = None,
                       *, complete: bool = False, run_final_hooks: bool = True,
                       **kwargs: Any) -> Dict[str, Any]:
        """Render calibrated output for the host or a non-final preview."""
        output = {} if output is None else output
        if not isinstance(output, dict):
            raise ValueError("final output must be a dictionary")
        contracts = self.bundle.pinned.get("contracts", {})
        decision = _sispis_calibration().calibrate(
            self.signals,
            calibration=contracts.get("sispis_calibration"),
            signal_schema=contracts.get("signal_schema"),
            signal_registry=contracts.get("signal_registry"))
        result = {
            **output,
            **kwargs,
            "sispis": decision,
        }
        if run_final_hooks:
            for hook in self.final_hooks:
                hook(result)
        if complete and self.lifecycle is not None:
            self.lifecycle.on_completion()
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
            self.lifecycle.on_response_complete()
            self.active_lifecycle = set(self.lifecycle.active)
        return result
    def preview_output(self, output: Optional[Dict[str, Any]] = None,
                       **kwargs: Any) -> Dict[str, Any]:
        """Render a pure preview without hooks or completion lifecycle."""
        return self._render_output(output, complete=False, run_final_hooks=False,
                                   **kwargs)
    def final_output(self, *_: Any, **__: Any) -> Dict[str, Any]:
        raise PermissionError("finalization requires a validated completion permit")
    def finish_task(self, output: Optional[Dict[str, Any]] = None,
                    *, permit: Optional[CompletionPermit] = None,
                    **kwargs: Any) -> Dict[str, Any]:
        """Finalize only with a current completion permit and closed segments."""
        permit = permit or self.completion_permit
        if permit is None:
            raise PermissionError("task finalization requires a validated completion permit")
        if not self._authoritative_state_ready():
            raise PermissionError("shared StateWork authority is unavailable for completion")
        if not self._revocation_current():
            raise PermissionError("pinned policy has been immediately revoked")
        if (permit.task_id != self.bundle.task_id
                or permit.policy_hash != self.bundle.pinned["policy_hash"]
                or permit.session_nonce != self.session_nonce
                or permit is not self.completion_permit):
            raise PermissionError("completion permit is not bound to this runtime session")
        if permit.authority_revision != self.authority_revision:
            raise PermissionError("completion permit predates a later authority revision")
        try:
            self.ledger.resolve(list(permit.evidence_refs), subject_ref=permit.subject_ref,
                                allowed_kinds={"boundary_evidence", "completion_boundary"})
            boundary_records = [self.ledger.get(ref) for ref in permit.evidence_refs]
            if any(record is None or record.invalidated or not record.claim_digest
                   or not record.invocation_id for record in boundary_records):
                raise ValueError("completion permit evidence is no longer current")
            for record in boundary_records:
                expected = completion_claim_digest(
                    self.bundle.task_id, permit.subject_ref, permit.boundary_type,
                    permit.boundary_target, permit.expected_validator,
                    record.invocation_id or "", permit.goal_contract_hash,
                    tuple(permit.criterion_ids), tuple(permit.criterion_evidence_refs))
                if record.claim_digest != permit.claim_digest or record.claim_digest != expected:
                    raise ValueError("completion permit evidence claim binding changed")
        except ValueError as exc:
            raise PermissionError(f"completion permit evidence is invalid: {exc}") from exc
        if any(not record["resolved"] for record in self.contradictions.values()):
            raise PermissionError("cannot finalize with unresolved operator contradictions")
        if len(self.execution_segments) == 1 and self.current_segment.statework_id is None:
            self._segment_status[self.current_segment.segment_id] = SegmentStatus.COMPLETED
        if any(status != SegmentStatus.COMPLETED for status in self._segment_status.values()):
            raise PermissionError("cannot finalize before every execution segment completes")
        goal_contract = self.bundle.pinned.get("goal_contract") or {}
        required_criterion_ids = {
            str(item.get("criterion_id"))
            for item in goal_contract.get("acceptance_criteria", ())
            if item.get("required", True)}
        if (permit.goal_contract_hash != self.bundle.pinned.get("goal_contract_hash", "")
                or not required_criterion_ids.issubset(set(permit.criterion_ids))):
            raise PermissionError("completion permit does not prove the pinned task contract")
        try:
            self._validate_criterion_proofs(
                permit.criterion_ids, permit.criterion_evidence_refs, permit.subject_ref)
        except ValueError as exc:
            raise PermissionError(f"completion permit criterion evidence is invalid: {exc}") from exc
        for statework in self.bundle.stateworks:
            statework_id = str(statework.get("id") or "")
            subject_ref = str(statework.get("subject_ref") or permit.subject_ref)
            contract_hash = str(statework.get("workflow_contract_hash") or "")
            complete = False
            if self.state_authority is not None and contract_hash:
                snapshot = self.state_authority.inspect_workflow(
                    statework_id=statework_id, subject_ref=subject_ref,
                    contract_hash=contract_hash)
                complete = bool(snapshot.completed)
            else:
                workflow_fact = next((item for item in self.metacognitive_state.workflows
                                      if item.statework_id == statework_id
                                      and item.subject_ref == subject_ref), None)
                complete = bool(workflow_fact and workflow_fact.completed)
            if not complete:
                raise PermissionError(
                    f"cannot finalize before StateWork {statework_id!r} is complete")
        self._journal("task_finished", subject_ref=permit.subject_ref,
                      claim_digest=permit.claim_digest)
        return self._render_output(output, complete=True, **kwargs)
