"""Deterministic standalone behavior fallback for a CFW session."""
from __future__ import annotations

import importlib.util
import sys

from runtime_contract.paths import runtime_asset_root
from runtime_contract.task import task_goal_contract_mapping_hash
from runtime_contract.task_evidence import CompletionReadiness
from runtime_contract.behavior_facts import ActivityFact, AttemptSemanticDescriptor
from runtime_contract.standalone_attention import StandaloneAttentionState
from runtime_session.standalone_behavior_policy import (
    StandaloneBehaviorDecision, StandaloneBehaviorFacts,
)

ROOT = runtime_asset_root()
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
    """Session method boundary for deterministic CFW fallback behavior."""
    def _standalone_attention(self) -> StandaloneAttentionState:
        """Return the fallback-only attention state aligned to local criteria."""
        authority = self.ensure_standalone_behavior_authority()
        if authority is None:
            raise RuntimeError("standalone attention is unavailable under external authority")
        authority.attention_state.sync_criteria(self.behavior.task_evidence.criteria)
        return authority.attention_state

    def _journal_standalone_attention(self) -> None:
        if not self.behavior.is_external:
            self._journal("standalone_attention_state_changed",
                          snapshot=self._standalone_attention().snapshot())

    def _record_standalone_activity(self, fact: ActivityFact | None) -> None:
        if fact is None or self.behavior.is_external:
            return
        self._standalone_attention().record_activity(fact)
        self._journal_standalone_attention()

    def _record_standalone_failed_approach(
            self, descriptor: AttemptSemanticDescriptor) -> None:
        if self.behavior.is_external:
            return
        attention = self._standalone_attention()
        attention.record_failed_approach(
            descriptor, sequence=self.behavior.task_evidence.next_sequence)
        self._journal_standalone_attention()

    def _evaluate_standalone_behavior(self) -> StandaloneBehaviorDecision:
        if self.behavior.is_external:
            state = self.effective_external_behavior_projection()
            return StandaloneBehaviorDecision(
                str(state.get("cognitive_mode") or "DELIVER"),
                "behavior policy is owned by DigitalPsychology")
        self.ensure_standalone_behavior_authority()
        goal = self.bundle.pinned.get("goal_contract") or {}
        pinned_goal_hash = str(self.bundle.pinned.get("goal_contract_hash") or "")
        goal_anchor_valid = (
            goal.get("work_mode") != "delivery"
            or bool(goal and pinned_goal_hash
                    and task_goal_contract_mapping_hash(goal) == pinned_goal_hash))
        verification = goal.get("verification_policy") or {}
        attention = self._standalone_attention()
        state = self.attempts.controller.state("primary-objective") if self.attempts.controller else None
        required_criteria = [item for item in goal.get("acceptance_criteria", ())
                             if item.get("required", True)]
        completion_readiness = None
        if required_criteria:
            criterion_states = {item.criterion_id: item
                                for item in self.behavior.task_evidence.criteria}
            criteria_satisfied = all(
                criterion_states.get(str(item.get("criterion_id"))) is not None
                and criterion_states[str(item.get("criterion_id"))].status == "satisfied"
                for item in required_criteria)
            workflow_facts = {
                (item.statework_id, item.subject_ref): item
                for item in self.behavior.task_evidence.workflows}
            workflows_complete = True
            for item in self.bundle.stateworks:
                statework_id = str(item.get("id") or "")
                subject_ref = str(item.get("subject_ref")
                                  or self.bundle.policy.subject_ref or "")
                workflow = workflow_facts.get((statework_id, subject_ref))
                if workflow is None or workflow.completed is not True:
                    workflows_complete = False
                    break
            completion_subject = (self.lifecycle_workflow.current_segment.subject_ref
                                  or self.bundle.policy.subject_ref)
            boundary_evidence_available = any(
                record.issuer.startswith("validator:")
                and record.invocation_id is not None
                and bool(record.claim_digest)
                for record in self.evidence.ledger.current_records(
                    subject_ref=completion_subject,
                    allowed_kinds={"boundary_evidence", "completion_boundary"}))
            completion_readiness = CompletionReadiness(
                required_criteria_satisfied=criteria_satisfied,
                boundary_evidence_available=boundary_evidence_available,
                unresolved_contradictions=any(
                    not bool(item.get("resolved"))
                    and (completion_subject is None
                         or item.get("subject") == completion_subject)
                    for item in self.evidence.contradictions.values()),
                active_stateworks_complete=workflows_complete,
                pending_verification_obligations=len(
                    self.behavior.task_evidence.pending_verification_obligations))
        behavior_facts = StandaloneBehaviorFacts(
            active_criterion_ids=attention.active_attention_window(
                self.behavior.task_evidence.criteria),
            has_active_blockers=bool(self.behavior.task_evidence.blockers),
            pending_verification_obligations=len(
                self.behavior.task_evidence.pending_verification_obligations),
            progress_redirect_guidance_pending=attention.progress_redirect_guidance_pending,
            progress_redirected=attention.progress_redirected)
        decision = self.behavior.standalone_behavior_policy.decide(
            behavior_facts,
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
                not bool(item.get("resolved")) for item in self.evidence.contradictions.values()),
            goal_contract_anchor_valid=goal_anchor_valid,
            completion_readiness=completion_readiness)
        if decision != self.behavior.standalone_behavior_decision:
            self.behavior.set_standalone_behavior_decision(decision)
            self._journal("standalone_behavior_decision_recorded", decision=decision.to_dict(),
                          logical_revision=self.behavior.task_evidence.logical_revision)
        return decision

    def _evaluate_metacognition(self) -> StandaloneBehaviorDecision:
        """Compatibility alias for the former standalone evaluator name."""
        return self._evaluate_standalone_behavior()
