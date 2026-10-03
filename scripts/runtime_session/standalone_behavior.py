"""Standalone metacognitive and semantic provider behavior for a CFW session."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from runtime_contract.actions import ActionDescriptor
from runtime_contract.behavioral_fusion import SemanticFusion, fuse_action_relation
from runtime_contract.metacognition import CompletionReadiness, MetaDecision, SemanticSignals
from runtime_contract.system_one import (
    ActivityFact, SystemOneFrame, SystemOneIdentity, SystemOneJudgment,
    SystemOnePolicy,
)
from runtime_session.lifecycle import _canonical_system_one_status

ROOT = Path(__file__).resolve().parents[2]
_SISPIS_CALIBRATION = None


def _sispis_calibration():
    global _SISPIS_CALIBRATION
    if _SISPIS_CALIBRATION is None:
        module_path = ROOT / "SISPIS" / "runtime" / "calibrate.py"
        spec = importlib.util.spec_from_file_location("cfw_sispis_calibration", module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cfw_sispis_calibration"] = module
        spec.loader.exec_module(module)
        _SISPIS_CALIBRATION = module
    return _SISPIS_CALIBRATION


class StandaloneBehaviorMethods:
    """Session method boundary for CFW-owned semantic and metacognitive control."""
    def _system_one_policy(self) -> SystemOnePolicy:
        return SystemOnePolicy(**dict(self.bundle.pinned.get("system_one_policy") or {}))
    def _evaluate_metacognition(self, *,
                                 semantic: Optional[SemanticSignals] = None) -> MetaDecision:
        if self.external_behavior_authority:
            state = self.effective_external_behavior_projection()
            return MetaDecision(
                str(state.get("cognitive_mode") or "DELIVER"),
                "behavior policy is owned by DigitalPsychology")
        self.ensure_standalone_behavior_authority()
        goal = self.bundle.pinned.get("goal_contract") or {}
        verification = goal.get("verification_policy") or {}
        state = self.convergence.state("primary-objective") if self.convergence else None
        required_criteria = [item for item in goal.get("acceptance_criteria", ())
                             if item.get("required", True)]
        completion_readiness = None
        if required_criteria:
            criterion_states = {item.criterion_id: item
                                for item in self.metacognitive_state.criteria}
            criteria_satisfied = all(
                criterion_states.get(str(item.get("criterion_id"))) is not None
                and criterion_states[str(item.get("criterion_id"))].status == "satisfied"
                for item in required_criteria)
            workflow_facts = {
                (item.statework_id, item.subject_ref): item
                for item in self.metacognitive_state.workflows}
            workflows_complete = True
            for item in self.bundle.stateworks:
                statework_id = str(item.get("id") or "")
                subject_ref = str(item.get("subject_ref")
                                  or self.bundle.policy.subject_ref or "")
                workflow = workflow_facts.get((statework_id, subject_ref))
                if workflow is None or workflow.completed is not True:
                    workflows_complete = False
                    break
            completion_subject = (self.current_segment.subject_ref
                                  or self.bundle.policy.subject_ref)
            boundary_evidence_available = any(
                record.issuer.startswith("validator:")
                and record.invocation_id is not None
                and bool(record.claim_digest)
                for record in self.ledger.current_records(
                    subject_ref=completion_subject,
                    allowed_kinds={"boundary_evidence", "completion_boundary"}))
            completion_readiness = CompletionReadiness(
                required_criteria_satisfied=criteria_satisfied,
                boundary_evidence_available=boundary_evidence_available,
                unresolved_contradictions=any(
                    not bool(item.get("resolved"))
                    and (completion_subject is None
                         or item.get("subject") == completion_subject)
                    for item in self.contradictions.values()),
                active_stateworks_complete=workflows_complete,
                pending_verification_obligations=len(
                    self.metacognitive_state.pending_verification_obligations))
        current_semantic = (semantic if semantic is not None else
                            self.semantic_signal_store.current(
                                metacognitive_revision=self.metacognitive_state.logical_revision,
                                question_families={"action_relation", "progress_relation",
                                                   "retry_relation", "goal_drift"})
                            if self.semantic_signals_revision
                            == self.metacognitive_state.logical_revision else SemanticSignals())
        decision = self.metacognitive_controller.evaluate(
            self.metacognitive_state,
            support_actions_since_progress=(
                state.support_actions_since_goal_progress if state else 0),
            support_action_limit=int(verification.get(
                "support_actions_before_goal_reanchor", 5) or 5),
            total_actions_since_progress=(
                state.total_actions_since_goal_progress if state else 0),
            total_action_limit=int(verification.get(
                "max_committed_actions_without_goal_progress", 15) or 15),
            recovery_epochs_since_progress=(
                state.recovery_epochs_since_goal_progress if state else 0),
            max_recovery_epochs=int(verification.get(
                "max_recovery_epochs_without_goal_progress", 3) or 3),
            same_approach_retries=(state.same_approach_retries if state else 0),
            max_same_approach_retries=int(verification.get(
                "max_same_approach_retries", 2) or 2),
            unresolved_contradiction=any(
                not bool(item.get("resolved")) for item in self.contradictions.values()),
            semantic=current_semantic,
            completion_readiness=completion_readiness)
        if decision != self.metacognitive_decision:
            object.__setattr__(self, "metacognitive_decision", decision)
            self._journal("metacognitive_decision_recorded", decision=decision.to_dict(),
                          logical_revision=self.metacognitive_state.logical_revision)
        return decision
    def _system_one_query(self, *, questions: tuple[str, ...], candidate: ActivityFact,
                          subject_ref: Optional[str] = None,
                          action_request_id: Optional[str] = None,
                          action_invocation_id: Optional[str] = None,
                          descriptor: Optional[ActionDescriptor] = None,
                          route_candidates: tuple[str, ...] = (),
                          route_state_revision: Optional[int] = None,
                          route_statework_id: Optional[str] = None) -> Optional[SystemOneJudgment]:
        """Serialize a complete provider operation for this RuntimeSession identity."""
        with self._system_one_provider_lock:
            return self._system_one_query_serialized(
                questions=questions, candidate=candidate, subject_ref=subject_ref,
                action_request_id=action_request_id,
                action_invocation_id=action_invocation_id, descriptor=descriptor,
                route_candidates=route_candidates,
                route_state_revision=route_state_revision,
                route_statework_id=route_statework_id)
    def _system_one_query_serialized(self, *, questions: tuple[str, ...], candidate: ActivityFact,
                          subject_ref: Optional[str] = None,
                          action_request_id: Optional[str] = None,
                          action_invocation_id: Optional[str] = None,
                          descriptor: Optional[ActionDescriptor] = None,
                          route_candidates: tuple[str, ...] = (),
                          route_state_revision: Optional[int] = None,
                          route_statework_id: Optional[str] = None) -> Optional[SystemOneJudgment]:
        """Query from a typed host snapshot after authority checks."""
        if self.external_behavior_authority:
            return None
        self.ensure_standalone_behavior_authority()
        policy = self._system_one_policy()
        if policy.mode == "disabled":
            return None
        enabled_questions = set(policy.enabled_question_families)
        questions = tuple(question for question in questions
                          if question in enabled_questions)
        if not questions:
            return None
        goal = self.bundle.pinned.get("goal_contract") or {}
        if not goal.get("objective") or self.system_one_judge is None:
            return None
        with self._system_one_lock:
            object.__setattr__(self, "system_one_sequence", self.system_one_sequence + 1)
            sequence = self.system_one_sequence
        self._journal("system_one_sequence", sequence=sequence)
        identity = SystemOneIdentity(
            namespace_id=self.execution_identity.namespace_id,
            application_id=str(self.bundle.pinned.get("application_id") or "unknown-application"),
            application_instance_id=self.execution_identity.application_instance_id,
            agent_id=str(self.bundle.pinned.get("agent_id") or "agent"),
            agent_instance_id=self.execution_identity.agent_instance_id,
            session_id=self.execution_identity.session_id,
            task_id=self.execution_identity.task_id,
            attempt_id=self.execution_identity.attempt_id,
            interaction_id=self.execution_identity.interaction_id)
        convergence_state = self.convergence.state("primary-objective") if self.convergence else None
        convergence = {
            "support_actions_since_goal_progress": int(
                convergence_state.support_actions_since_goal_progress if convergence_state else 0),
            "recovery_epochs_since_goal_progress": int(
                convergence_state.recovery_epochs_since_goal_progress if convergence_state else 0),
            "same_approach_retries": int(
                convergence_state.same_approach_retries if convergence_state else 0),
        }
        active_flow = None
        active_statework_id = self.current_segment.statework_id
        if active_statework_id:
            active_statework = next((item for item in self.bundle.stateworks
                                     if item.get("id") == active_statework_id), None)
            selected_flow = active_statework.get("selected_flow") if active_statework else None
            if isinstance(selected_flow, (list, tuple)) and selected_flow:
                active_flow = Path(str(selected_flow[0])).parent.name
        query_meta_revision = self.metacognitive_state.logical_revision
        frame = self.system_one_projector.project(
            meta_state=self.metacognitive_state,
            identity=identity, sequence=sequence,
            policy_hash=str(self.bundle.pinned.get("policy_hash") or ""),
            policy=policy, questions=questions, goal_contract=goal,
            convergence=convergence, candidate=candidate,
            phase=self.bundle.pinned.get("phase"),
            active_segment=self.current_segment.segment_id,
            active_statework=(active_statework_id or None), active_flow=active_flow,
            current_state=(self.statework_states.get((
                active_statework_id or "", self.current_segment.subject_ref or ""))),
            blockers=tuple(f"{item.blocker_id}: {item.summary}" if item.summary
                           else item.blocker_id for item in self.metacognitive_state.blockers),
            failed_approaches=tuple(
                self.attempt_semantics[attempt_ref]
                for attempt_ref in self.attempt_descriptors
                if attempt_ref in self.attempt_failures
                and attempt_ref in self.attempt_semantics)[-4:],
            legal_routes=route_candidates)
        status_reason = "fallback"
        calibrated_questions: set[str] = set()
        judgment: Optional[SystemOneJudgment] = None
        try:
            # This lock is per RuntimeSession identity: stateful providers see
            # each full snapshot and its answer validation as one operation.
            with self._system_one_provider_lock:
                raw = self.system_one_judge.judge(frame, mode=policy.mode)
                if hasattr(raw, "judgment"):
                    status_reason = str(
                        getattr(raw, "reason", None)
                        or getattr(raw, "status", "fallback"))[:64]
                    calibration_hint = bool(getattr(raw, "calibrated", False))
                    calibrated_questions.update(
                        getattr(raw, "calibrated_question_ids", ()) or ())
                    raw = getattr(raw, "judgment", None)
                else:
                    calibration_hint = False
                if raw is not None:
                    judgment = (raw if isinstance(raw, SystemOneJudgment)
                                else SystemOneJudgment.from_mapping(
                                    raw.to_dict() if hasattr(raw, "to_dict") else raw))
                    self._validate_system_one_judgment(frame, judgment)
                    if calibration_hint and not calibrated_questions:
                        calibrated_questions.update(frame.question_ids)
                    calibrated_questions = {
                        question_id for question_id in calibrated_questions
                        if question_id in frame.question_ids
                        and (answer := judgment.answer(question_id)) is not None
                        and answer.calibrated_probabilities is not None
                        and answer.calibration_hash is not None
                    }
        except Exception:
            judgment, calibrated_questions = None, set()
            status_reason = "provider_exception"
        if (sequence != self.system_one_sequence
                or query_meta_revision != self.metacognitive_state.logical_revision):
            judgment, calibrated_questions = None, set()
            status_reason = "stale_response"
        status = _canonical_system_one_status(
            status_reason, has_judgment=judgment is not None, mode=policy.mode)
        calibrated = frozenset(calibrated_questions)

        relation = None
        influenced = False
        if descriptor is not None and "action_relation" in questions:
            fused = fuse_action_relation(
                descriptor, judgment=judgment, policy=policy, calibrated=calibrated)
            relation = fused.relation
            influenced = fused.model_influenced
        signals = SemanticFusion.signals(
            judgment, policy=policy, calibrated=calibrated)
        prior_decision = self.metacognitive_decision
        candidate_digest = hashlib.sha256(json.dumps({
            "candidate": candidate.to_dict(),
            "legal_routes": sorted(route_candidates),
        }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        scenario = (f"route:{route_statework_id}" if route_statework_id else
                    f"action:{action_request_id or action_invocation_id}" if action_request_id or action_invocation_id else
                    f"task:{self.bundle.task_id}")
        answer_by_family = {family: (judgment.answer(family) if judgment else None)
                            for family in questions}
        for family, field_name in {
                "action_relation": "action_relation",
                "progress_relation": "progress_relation",
                "retry_relation": "equivalent_retry",
                "goal_drift": "possible_goal_drift",
                "route_preference": "route_preference"}.items():
            value = getattr(signals, field_name)
            if value is None:
                continue
            answer = answer_by_family.get(family)
            self.semantic_signal_store.record(
                question_family=family, value=value, scenario=scenario,
                criterion_id=(candidate.affected_criterion_ids[0]
                              if candidate.affected_criterion_ids else None),
                cause_id="primary-objective", candidate_digest=candidate_digest,
                metacognitive_revision=query_meta_revision,
                source_decision_id=(judgment.request_id if judgment else frame.request_id),
                confidence=(answer.calibrated_confidence if answer else None))
        refreshed_signals = self.semantic_signal_store.current(
            metacognitive_revision=query_meta_revision,
            question_families={"action_relation", "progress_relation", "retry_relation",
                               "goal_drift", "route_preference"})
        object.__setattr__(self, "semantic_signals", refreshed_signals)
        object.__setattr__(self, "semantic_signals_revision",
                           query_meta_revision)
        self._journal("semantic_signal_store_changed",
                      records=self.semantic_signal_store.snapshot(),
                      metacognitive_revision=query_meta_revision)
        next_decision = self._evaluate_metacognition(semantic=refreshed_signals)
        control_changed = (next_decision.mode, next_decision.reason,
                           next_decision.goal_reanchor_required) != (
                               prior_decision.mode, prior_decision.reason,
                               prior_decision.goal_reanchor_required)
        semantic_influenced = bool(next_decision.semantic_attention_note)

        question_hash = frame.question_bundle_digest
        decision_id = frame.request_id
        answers = ({answer.question_id: answer.to_dict()
                    for answer in judgment.answers} if judgment else {})
        decision = {
            "decision_id": decision_id, "subject_ref": subject_ref,
            "action_request_id": action_request_id,
            "action_invocation_id": action_invocation_id,
            "sequence": frame.sequence, "state_digest": frame.state_digest,
            "request_digest": frame.request_digest,
            "policy_hash": frame.policy_hash,
            "legal_routes": list(route_candidates),
            "route_state_revision": route_state_revision,
            "route_statework_id": route_statework_id,
            "question_hash": question_hash,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "judgment": judgment.to_dict() if judgment else {},
            "answers": answers, "status": status,
            "status_reason": status_reason,
            "calibrated": bool(calibrated),
            "calibrated_question_ids": sorted(calibrated),
            "influenced": influenced,
            "semantic_signals": signals.to_dict(),
            "metacognitive_decision": next_decision.to_dict(),
            "system_one_semantic_influenced": semantic_influenced,
            "metacognitive_control_changed": control_changed,
            "control_change_source": ("deterministic_metacognition"
                                      if control_changed else None),
            "fused_action_relation": relation,
        }
        self.system_one_decisions[decision_id] = decision
        while len(self.system_one_decisions) > 512:
            oldest = next(iter(self.system_one_decisions))
            self.system_one_decisions.pop(oldest)
            self.system_one_outcomes.difference_update(
                key for key in tuple(self.system_one_outcomes)
                if key.startswith(f"{oldest}:"))
        self._journal("system_one_decision_recorded", decision=decision)
        payload = {
            "summary": "bounded System-1 semantic judgment" if judgment else "System-1 fallback",
            "system_one_decision_id": decision_id,
            "system_one_mode": policy.mode,
            "system_one_backend_id": policy.backend_id,
            "system_one_model_id": policy.model_id,
            "system_one_model_revision": policy.model_revision,
            "system_one_model_content_hash": policy.model_content_hash,
            "system_one_provider_protocol_hash": policy.provider_protocol_hash,
            "system_one_protocol_version": policy.protocol_version,
            "system_one_protocol_hash": policy.protocol_hash,
            "system_one_question_registry_hash": policy.question_registry_hash,
            "system_one_state_projection_schema_hash": policy.state_projection_schema_hash,
            "system_one_calibration_hash": policy.calibration_bundle_hash,
            "system_one_work_mode": str(goal.get("work_mode") or "delivery"),
            "system_one_deliverable_kind": str(goal.get("deliverable_kind") or "code_behavior"),
            "system_one_sequence": frame.sequence,
            "system_one_state_digest": frame.state_digest,
            "system_one_question_bundle_hash": question_hash,
            "system_one_status": status,
            "system_one_status_reason": status_reason,
            "system_one_influenced": influenced,
            "system_one_semantic_influenced": semantic_influenced,
            "metacognitive_control_changed": control_changed,
            "control_change_source": ("deterministic_metacognition"
                                      if control_changed else None),
            "source_decision_ids": list(signals.source_decisions),
            "system_one_answers": answers,
            "system_one_legal_routes": sorted(route_candidates),
            "system_one_domain_tags": sorted(
                map(str, frame.state.task.get("domain_tags") or ())),
        }
        if action_request_id:
            payload["action_request_id"] = action_request_id
        if action_invocation_id:
            payload["action_invocation_id"] = action_invocation_id
        if relation is not None:
            payload["system_one_fused_action_relation"] = relation
        if judgment is not None:
            event_labels = {
                "action_relation": "system_one_action_relation",
                "progress_relation": "system_one_progress_relation",
                "retry_relation": "system_one_retry_relation",
                "goal_drift": "system_one_goal_drift",
                "route_preference": "system_one_route_preference",
            }
            for answer in judgment.answers:
                target = event_labels.get(answer.question_id)
                if target:
                    payload[target] = answer.selected
        self._emit_runtime({"category": "decision", "event_type": "system_one_decision",
                            "subject": subject_ref, "payload": payload})
        return judgment
    @staticmethod
    def _validate_system_one_judgment(frame: SystemOneFrame,
                                      judgment: SystemOneJudgment) -> None:
        checks = (
            (judgment.identity == frame.identity, "identity"),
            (judgment.request_id == frame.request_id, "request ID"),
            (judgment.sequence == frame.sequence, "sequence"),
            (judgment.state_digest == frame.state_digest, "state digest"),
            (judgment.question_bundle_digest == frame.question_bundle_digest, "question bundle"),
            (judgment.request_digest == frame.request_digest, "request digest"),
            (judgment.policy_hash == frame.policy_hash, "policy"),
            (judgment.backend_id == frame.backend_id, "backend"),
            (judgment.model_id == frame.model_id, "model"),
            (judgment.model_revision == frame.model_revision, "model revision"),
            (judgment.model_content_hash == frame.model_content_hash, "model content"),
            (judgment.provider_protocol_hash == frame.provider_protocol_hash, "provider protocol"),
            (judgment.calibration_bundle_hash == frame.calibration_bundle_hash, "calibration"),
        )
        for passed, label in checks:
            if not passed:
                raise ValueError(f"System-1 response {label} mismatch")
        if set(answer.question_id for answer in judgment.answers) != set(frame.question_ids):
            raise ValueError("System-1 response question set mismatch")
        for spec in frame.questions:
            answer = judgment.answer(spec.question_id)
            assert answer is not None
            if tuple(item.label for item in answer.raw_probabilities) != tuple(
                    label for label, _ in spec.criteria):
                raise ValueError("System-1 probability labels differ from question options")
        route = judgment.answer("route_preference")
        if route is not None and route.selected not in frame.state.legal_routes:
            raise ValueError("System-1 route preference is outside eligible candidates")
