"""Attempt tracking and convergence evidence methods for CFW sessions."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from runtime_contract.convergence import AttemptDescriptor
from runtime_contract.metacognition import CriterionEvidenceBinding
from runtime_contract.system_one import AttemptSemanticDescriptor
from runtime_session.host_authority import _HostCapability


class AttemptMethods:
    """Attempt-local state, outcome, recovery, and progress recording."""
    def _retry_comparison_fingerprints(
            self, descriptor: AttemptDescriptor) -> tuple[str, str, Optional[str]]:
        if descriptor.attempt_ref not in self._semantic_retry_same or self.convergence is None:
            return (descriptor.action_plan_digest, descriptor.premise_digest,
                    descriptor.premise_digest)
        prior = self.convergence.state(descriptor.cause_id)
        return (prior.action_fingerprint or descriptor.action_plan_digest,
                prior.hypothesis_fingerprint or descriptor.premise_digest,
                prior.premise_digest or descriptor.premise_digest)
    def _record_convergence(self, reason: str) -> None:
        """Persist convergence state so resume cannot erase the budget."""
        if self.convergence is None:
            return
        self._journal("convergence_recorded", reason=reason,
                      convergence=self.convergence.export())
    def _system_one_decision_for_action(self, capability: _HostCapability,
                                        action_request_id: str) -> Optional[str]:
        self._require_host(capability)
        if (not isinstance(action_request_id, str)
                or not action_request_id.startswith("action-request-")
                or len(action_request_id) > 256):
            raise ValueError("action_request_id must be a bounded host-generated identifier")
        matches = [decision for decision in self.system_one_decisions.values()
                   if decision.get("action_request_id") == action_request_id
                   and decision.get("judgment")]
        if not matches:
            return None
        matches.sort(key=lambda item: int(item.get("sequence", 0)))
        return str(matches[-1]["decision_id"])
    def _record_system_one_outcome(self, capability: _HostCapability, *,
                                   decision_id: str, label: str, value: str,
                                   evidence_ref: str, authority: str,
                                   criterion_id: Optional[str] = None) -> str:
        """Append T2 ground truth only from later host-attested evidence."""
        self._require_host(capability)
        decision = self.system_one_decisions.get(decision_id)
        if decision is None or not decision.get("judgment"):
            raise ValueError("System-1 outcome requires a recorded prediction")
        outcome_key = f"{decision_id}:{label}"
        if outcome_key in self.system_one_outcomes:
            raise ValueError("System-1 outcome already recorded for this label")
        label_values = {
            "action_relation": {"advances_primary_goal", "removes_concrete_blocker",
                                "required_verification", "diagnostic_support", "unrelated"},
            "progress_relation": {"goal_progress", "blocker_removed", "diagnostic_progress",
                                  "verification_progress", "recovery_progress", "no_progress"},
            "retry_relation": {"materially_new", "same_premise_new_action",
                               "equivalent_retry", "unrelated"},
            "goal_drift": {"yes", "no"},
        }
        prediction = next((item for item in decision["judgment"].get("answers", ())
                           if item.get("question_id") == label), None)
        if prediction is None:
            raise ValueError("outcome label was not included in the prediction")
        if label == "route_preference":
            label_values[label] = {str(item.get("label")) for item in
                                   prediction.get("raw_probabilities", ())
                                   if isinstance(item, Mapping)}
        if label not in label_values or value not in label_values[label]:
            raise ValueError("unsupported System-1 ground-truth label")
        if authority not in {"host_evidence", "validator", "csw_fact", "operator"}:
            raise ValueError("unsupported System-1 outcome authority")
        record = self.ledger.get(evidence_ref)
        if record is None or record.invalidated:
            raise ValueError("System-1 outcome requires current host-attested evidence")
        expected_subject = decision.get("subject_ref") or self.bundle.policy.subject_ref
        if expected_subject and record.subject_ref != expected_subject:
            raise ValueError("System-1 outcome evidence subject does not match its decision")
        issuer = str(record.issuer or "")
        issuer_prefixes = {
            "host_evidence": ("host:", "runtime:"),
            "validator": ("validator:",),
            "csw_fact": ("statework:", "csw:"),
            "operator": ("operator:",),
        }
        if not issuer.startswith(issuer_prefixes[authority]):
            raise PermissionError("evidence issuer does not match declared outcome authority")
        observed = datetime.fromisoformat(str(record.observed_at).replace("Z", "+00:00"))
        decided = datetime.fromisoformat(str(decision["timestamp"]).replace("Z", "+00:00"))
        if observed <= decided:
            raise ValueError("System-1 ground truth must be observed after its prediction")
        if label == "progress_relation" and value in {"goal_progress", "blocker_removed"}:
            criteria = {str(item.get("criterion_id")) for item in
                        (self.bundle.pinned.get("goal_contract") or {}).get(
                            "acceptance_criteria", ())}
            if not criterion_id or criterion_id not in criteria:
                raise ValueError("goal progress outcome must match a pinned acceptance criterion")
        elif criterion_id is not None:
            criteria = {str(item.get("criterion_id")) for item in
                        (self.bundle.pinned.get("goal_contract") or {}).get(
                            "acceptance_criteria", ())}
            if criterion_id not in criteria:
                raise ValueError("outcome references an unknown acceptance criterion")
        self.system_one_outcomes.add(outcome_key)
        outcome_event_id = f"system-one-outcome-{uuid.uuid4()}"
        self._journal("system_one_outcome_recorded", decision_id=decision_id,
                      label=label, value=value, evidence_ref=evidence_ref,
                      authority=authority, outcome_event_id=outcome_event_id)
        label_key = {
            "action_relation": "system_one_outcome_action_relation",
            "progress_relation": "system_one_outcome_progress_relation",
            "retry_relation": "system_one_outcome_retry_relation",
            "goal_drift": "system_one_outcome_goal_drift",
            "route_preference": "system_one_outcome_route_preference",
        }[label]
        self._emit_runtime({
            "event_id": outcome_event_id,
            "category": "validation", "event_type": "system_one_outcome",
            "subject": record.subject_ref, "evidence_refs": [evidence_ref],
            "payload": {"summary": "independent System-1 evaluation outcome",
                        "system_one_outcome_for": decision_id,
                        label_key: value,
                        "system_one_outcome_authority": authority,
                        "system_one_outcome_evidence_ref": evidence_ref,
                        "system_one_independent_outcome": True,
                        "criterion_id": criterion_id or ""}})
        return outcome_event_id
    def _shared_branch_exhausted(self, descriptor: AttemptDescriptor,
                                 recovery_limit: int) -> bool:
        membership = self.shared_workflow_membership or {}
        if self.shared_branch_ledger is None or not membership.get("workflow_id"):
            return False
        count = self.shared_branch_ledger.recovery_count(
            namespace_id=str(membership.get("namespace_id") or ""),
            application_instance_id=str(membership.get("application_instance_id") or ""),
            workflow_id=str(membership.get("workflow_id") or ""),
            cause_id=descriptor.cause_id, premise_digest=descriptor.premise_digest)
        if count < recovery_limit:
            return False
        if self.convergence is not None:
            state = self.convergence.state(descriptor.cause_id)
            state.disposition = "BLOCKED"
            state.terminated_reason = "shared_premise_recovery_limit"
            self._record_convergence("shared_premise_recovery_limit")
        return True
    def _begin_attempt(self, capability: _HostCapability, *, attempt_ref: str,
                       cause_id: str, premise_digest: str,
                       action_plan: Mapping[str, Any],
                       parent_attempt_ref: Optional[str] = None,
                       semantic_descriptor: Optional[Mapping[str, Any]] = None) -> AttemptDescriptor:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("attempt tracking requires the host convergence controller")
        if not isinstance(action_plan, Mapping):
            raise TypeError("action_plan must be host-accepted structured data")
        encoded = json.dumps(dict(action_plan), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("action_plan exceeds the bounded attempt contract")
        descriptor = AttemptDescriptor(
            attempt_ref=attempt_ref, cause_id=cause_id,
            premise_digest=premise_digest,
            action_plan_digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            parent_attempt_ref=parent_attempt_ref)
        semantic = None
        if semantic_descriptor is not None:
            if not isinstance(semantic_descriptor, Mapping):
                raise TypeError("semantic_descriptor must be a bounded host-accepted mapping")
            allowed = {"cause_class", "hypothesis_summary", "mechanism", "evidence_target",
                       "planned_action_class", "planned_target_kind", "failed_result_class",
                       "affected_criterion_ids", "source"}
            if set(semantic_descriptor) - allowed:
                raise ValueError("semantic_descriptor contains unknown fields")
            semantic = AttemptSemanticDescriptor(
                cause_class=str(semantic_descriptor.get("cause_class") or cause_id)[:64],
                hypothesis_summary=str(semantic_descriptor.get("hypothesis_summary") or "")[:256],
                mechanism=str(semantic_descriptor.get("mechanism") or "")[:256],
                evidence_target=str(semantic_descriptor.get("evidence_target") or "")[:128],
                planned_action_class=str(semantic_descriptor.get("planned_action_class") or "")[:64],
                planned_target_kind=str(semantic_descriptor.get("planned_target_kind") or "")[:64],
                failed_result_class=str(semantic_descriptor.get("failed_result_class") or "pending")[:64],
                premise_digest=premise_digest,
                action_plan_digest=descriptor.action_plan_digest,
                affected_criterion_ids=tuple(semantic_descriptor.get("affected_criterion_ids") or ()),
                source=str(semantic_descriptor.get("source") or "model_proposed"))
        prior = self.attempt_descriptors.get(attempt_ref)
        if prior is not None and prior != descriptor:
            raise ValueError("attempt_ref is already bound to a different plan")
        self.attempt_descriptors[attempt_ref] = descriptor
        if semantic is not None:
            old_semantic = self.attempt_semantics.get(attempt_ref)
            if old_semantic is not None and old_semantic != semantic:
                raise ValueError("attempt_ref is already bound to a different semantic descriptor")
            self.attempt_semantics[attempt_ref] = semantic
        self._journal("attempt_descriptor_recorded", descriptor=descriptor.to_dict(),
                      semantic_descriptor=semantic.to_dict() if semantic else None)
        return descriptor
    def _record_attempt_failure(self, capability: _HostCapability, *,
                                attempt_ref: str, evidence_ref: str) -> None:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("attempt tracking requires the host convergence controller")
        descriptor = self.attempt_descriptors.get(attempt_ref)
        record = self.ledger.get(evidence_ref)
        if descriptor is None or record is None or record.invalidated:
            raise ValueError("attempt failure requires a registered attempt and current evidence")
        if attempt_ref in self.attempt_failures:
            return
        membership = self.shared_workflow_membership or {}
        if self.shared_branch_ledger is not None and membership.get("workflow_id"):
            self.shared_branch_ledger.record_failure(
                namespace_id=str(membership["namespace_id"]),
                application_instance_id=str(membership["application_instance_id"]),
                workflow_id=str(membership["workflow_id"]),
                cause_id=descriptor.cause_id,
                premise_digest=descriptor.premise_digest,
                action_plan_fingerprint=descriptor.action_plan_digest,
                evidence_ref=evidence_ref, attempt_ref=attempt_ref)
        self.convergence.record_failure(
            cause_id=descriptor.cause_id,
            action_fingerprint=descriptor.action_plan_digest,
            hypothesis_fingerprint=descriptor.premise_digest,
            evidence_ref=evidence_ref, premise_digest=descriptor.premise_digest)
        semantic = self.attempt_semantics.get(attempt_ref)
        if semantic is not None:
            failed = AttemptSemanticDescriptor(
                cause_class=semantic.cause_class,
                hypothesis_summary=semantic.hypothesis_summary,
                mechanism=semantic.mechanism,
                evidence_target=semantic.evidence_target,
                planned_action_class=semantic.planned_action_class,
                planned_target_kind=semantic.planned_target_kind,
                failed_result_class=str(self.host_invocations.get(
                    self.ledger.get(evidence_ref).invocation_id or "", {}).get(
                        "result_class") or semantic.failed_result_class),
                premise_digest=semantic.premise_digest,
                action_plan_digest=semantic.action_plan_digest,
                affected_criterion_ids=semantic.affected_criterion_ids,
                source=semantic.source)
            self.metacognitive_state.record_failed_approach(failed)
            self._journal("system_one_projection_changed",
                          snapshot=self.metacognitive_state.snapshot())
        self.attempt_failures.add(attempt_ref)
        self._journal("attempt_failure_recorded", attempt_ref=attempt_ref,
                      cause_id=descriptor.cause_id, evidence_ref=evidence_ref,
                      convergence=self.convergence.export())
        self._record_convergence("attempt_failure")
        self._emit_runtime({
            "category": "retry", "event_type": "attempt_failure_recorded",
            "subject": record.subject_ref,
            "evidence_refs": [evidence_ref],
            "payload": {"summary": "host observed an unsuccessful attempt",
                        "result_class": "failure",
                        "premise_digest": descriptor.premise_digest,
                        "action_fingerprint": descriptor.action_plan_digest[:64]}})
        return None
    def _record_reinspection(self, capability: _HostCapability, *,
                             cause_id: str, revision: int) -> None:
        self._require_host(capability)
        if self.convergence is None or revision < 1:
            raise ValueError("reinspection requires convergence state and a positive revision")
        self.convergence.record_reinspection(cause_id=cause_id, revision=revision)
        self._record_convergence("reinspection")
        self._emit_runtime({
            "category": "observation", "event_type": "reinspection_recorded",
            "subject": self.bundle.policy.subject_ref,
            "payload": {"summary": "host recorded a fresh inspection revision",
                        "result_class": "reinspection",
                        "revision": revision}})
    def _record_goal_progress(self, capability: _HostCapability, *,
                              cause_id: str, evidence_ref: str,
                              criterion_id: Optional[str] = None,
                              progress_class: str = "GOAL_PROGRESS") -> None:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("goal progress requires the host convergence controller")
        if progress_class not in {"GOAL_PROGRESS", "BLOCKER_REMOVED"}:
            raise ValueError("goal progress must be GOAL_PROGRESS or BLOCKER_REMOVED")
        record = self.ledger.get(evidence_ref)
        if record is None or record.invalidated:
            raise ValueError("goal progress requires current host-attested evidence")
        expected_subject = self.bundle.policy.subject_ref
        if expected_subject and record.subject_ref != expected_subject:
            raise ValueError("goal progress evidence subject does not match the active task")
        supported_kinds = {
            "tool_result", "validator_result", "repository_evidence", "repository_truth",
            "state_transition", "completion_boundary", "rendered_observation",
        }
        if record.kind not in supported_kinds:
            raise ValueError("goal progress requires criterion-bound host evidence of an accepted kind")
        if criterion_id is None:
            raise ValueError("goal progress requires a criterion evidence binding")
        criterion_contract = next((item for item in
            (self.bundle.pinned.get("goal_contract") or {}).get("acceptance_criteria", ())
            if str(item.get("criterion_id")) == criterion_id), None)
        if criterion_contract is None:
            raise ValueError("goal progress references an unknown acceptance criterion")
        expected_evidence_kinds = self._criterion_evidence_kinds(
            str(criterion_contract.get("kind") or ""))
        if expected_evidence_kinds and record.kind not in expected_evidence_kinds:
            raise ValueError("goal progress evidence kind does not match the criterion contract")
        if record.kind == "tool_result":
            invocation = next((item for item in self.host_invocations.values()
                               if item.get("evidence_id") == evidence_ref), None)
            actual = dict((invocation or {}).get("actual_result") or {})
            binding = dict((invocation or {}).get("objective_binding") or {})
            if (not invocation or invocation.get("accounting_lane") != "DIRECT"
                    or binding.get("purpose_class") not in {"DIRECT_GOAL", "BLOCKER_REMOVAL"}
                    or criterion_id not in set(binding.get("criterion_ids") or ())
                    or invocation.get("result_class") not in {"success", "completed", "changed"}
                    or actual.get("artifact_changed") is not True):
                raise ValueError(
                    "tool-result progress requires a successful direct action bound to the criterion "
                    "and host-confirmed artifact change")
        authority = (
            "validator" if str(record.issuer or "").startswith("validator:") else
            "operator" if str(record.issuer or "").startswith("operator:") else
            "csw_authoritative" if str(record.issuer or "").startswith("csw:") else
            "host_observed" if str(record.issuer or "").startswith("host:") else None)
        if authority is None:
            raise ValueError("goal progress evidence issuer is not a recognized host authority")
        criteria = {
            str(item.get("criterion_id")) for item in
            (self.bundle.pinned.get("goal_contract") or {}).get("acceptance_criteria", ())
        }
        if criteria and criterion_id is None:
            raise ValueError("goal progress must match a pinned acceptance criterion")
        if criterion_id is not None and criterion_id not in criteria:
            raise ValueError("goal progress references an unknown acceptance criterion")
        if criterion_id is None:
            raise ValueError("goal progress requires a criterion evidence binding")
        criterion = next((item for item in
                          (self.bundle.pinned.get("goal_contract") or {}).get(
                              "acceptance_criteria", ())
                          if str(item.get("criterion_id")) == criterion_id), None)
        if criterion is None:
            raise ValueError("goal progress criterion is not present in the pinned task contract")
        binding = CriterionEvidenceBinding(
            criterion_id=criterion_id, evidence_ref=evidence_ref,
            evidence_kind=record.kind, authority=authority,
            artifact_revision=record.content_digest)
        digest = hashlib.sha256(json.dumps({
            "evidence_ref": evidence_ref, "criterion_id": criterion_id,
            "content_digest": record.content_digest,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        progress_causes = {cause_id}
        if (self.bundle.pinned.get("goal_contract") or {}).get("work_mode") == "delivery":
            progress_causes.add("primary-objective")
        for progress_cause in progress_causes:
            self.convergence.record_progress(
                cause_id=progress_cause, progress_digest=digest,
                progress_class=progress_class, evidence_ref=evidence_ref)
        self._record_convergence("goal_progress")
        self.metacognitive_state.record_goal_progress(
            binding, claim=str(criterion.get("claim") or ""),
            kind=str(criterion.get("kind") or "acceptance"))
        self._journal("system_one_projection_changed",
                      snapshot=self.metacognitive_state.snapshot())
        self._emit_runtime({
            "category": "decision", "event_type": "goal_progress_recorded",
            "subject": record.subject_ref, "evidence_refs": [evidence_ref],
            "payload": {"summary": "host attested progress on the primary objective",
                        "result_class": "goal_progress",
                        "progress_class": progress_class,
                        "criterion_id": criterion_id or ""}})
    def _record_criterion_verified(self, capability: _HostCapability, *,
                                   criterion_id: str, evidence_ref: str) -> None:
        """Satisfy a criterion only after resolving its pinned evidence policy."""
        self._require_host(capability)
        goal = self.bundle.pinned.get("goal_contract") or {}
        criterion = next((item for item in goal.get("acceptance_criteria", ())
                          if str(item.get("criterion_id")) == criterion_id), None)
        if criterion is None:
            raise ValueError("verification references an unknown pinned criterion")
        allowed_kinds = self._criterion_evidence_kinds(str(criterion.get("kind") or ""))
        try:
            resolved = self.ledger.resolve(
                [evidence_ref], subject_ref=self.bundle.policy.subject_ref,
                allowed_kinds=allowed_kinds or None)
        except ValueError as exc:
            raise ValueError("criterion verification requires accepted current host evidence") from exc
        record = resolved[0]
        if str(criterion.get("verification_strength") or "host_observation") == "validator":
            if not str(record.get("issuer") or "").startswith("validator:"):
                raise ValueError("criterion requires validator-issued evidence")
        self.metacognitive_state.mark_criterion_satisfied(
            criterion_id, evidence_ref=evidence_ref,
            evidence_kind=str(record.get("kind") or ""))
        self._journal("system_one_projection_changed",
                      snapshot=self.metacognitive_state.snapshot())
        self._emit_runtime({
            "category": "validation", "event_type": "criterion_verified",
            "subject": record.get("subject_ref"), "evidence_refs": [evidence_ref],
            "payload": {"summary": "host evidence satisfied a pinned criterion",
                        "result_class": "verified", "criterion_id": criterion_id}})
    def _record_recovery_epoch(self, capability: _HostCapability, *,
                               cause_id: str, reason: str) -> None:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("recovery tracking requires the host convergence controller")
        state = self.convergence.record_recovery_epoch(cause_id=cause_id)
        self.metacognitive_state.record_recovery_epoch()
        self._journal("system_one_projection_changed",
                      snapshot=self.metacognitive_state.snapshot())
        self._record_convergence("recovery_epoch")
        self._emit_runtime({
            "category": "retry", "event_type": "recovery_epoch_recorded",
            "subject": self.bundle.policy.subject_ref,
            "payload": {"summary": reason[:512],
                        "result_class": "recovery",
                        "progress_class": "RECOVERY_PROGRESS",
                        "recovery_epochs_since_goal_progress":
                            state.recovery_epochs_since_goal_progress}})
        if state.recovery_epochs_since_goal_progress >= int(
                ((self.bundle.pinned.get("goal_contract") or {}).get(
                    "verification_policy") or {}).get(
                        "max_recovery_epochs_without_goal_progress", 3)):
            self.request_termination(
                capability, disposition="BLOCKED",
                reason=(reason or "Recovery limit reached without primary-goal progress.")[:512],
                cause_id=cause_id,
                evidence_refs=([state.last_failure_evidence_ref]
                               if state.last_failure_evidence_ref else ()))
    def _reject_investigative_path(self, capability: _HostCapability, *,
                                   attempt_ref: str, evidence_ref: str) -> None:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("path rejection requires the host convergence controller")
        descriptor = self.attempt_descriptors.get(attempt_ref)
        record = self.ledger.get(evidence_ref)
        if descriptor is None or record is None or record.invalidated:
            raise ValueError("path rejection requires a registered attempt and current evidence")
        self.convergence.reject_path(
            cause_id=descriptor.cause_id,
            action_fingerprint=descriptor.action_plan_digest,
            hypothesis_fingerprint=descriptor.premise_digest,
            evidence_ref=evidence_ref, premise_digest=descriptor.premise_digest,
            observed_at=record.observed_at)
        self._record_convergence("investigative_path_rejected")
        self._emit_runtime({
            "category": "correction", "event_type": "investigative_path_rejected",
            "subject": record.subject_ref, "evidence_refs": [evidence_ref],
            "payload": {"summary": "host rejected an investigative branch",
                        "result_class": "rejected",
                        "premise_digest": descriptor.premise_digest,
                        "action_fingerprint": descriptor.action_plan_digest[:64]}})
    def _record_decision(self, capability: _HostCapability, event_type: str,
                         subject_ref: Optional[str], result_class: str) -> str:
        """Record a typed host-observed decision outcome."""
        self._require_host(capability)
        return self._host_event({"event_id": f"evt-{uuid.uuid4()}",
                                 "category": "decision", "event_type": event_type,
                                 "subject": subject_ref,
                                 "payload": {"result_class": result_class[:64]}})
