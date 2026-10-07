"""Deprecated state projection for legacy semantic assessment artifacts.

Live CFW runtime does not construct or expose this projection.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence
import uuid

from .metacognition import MetacognitiveState
from .protocol_artifact import QUESTION_REGISTRY
from .behavior_facts import (ActivityFact, AttemptSemanticDescriptor, CriterionState)
from .system_one import (QuestionSpec, SemanticState, SystemOneFrame,
                         SystemOneIdentity, SystemOnePolicy, canonical_hash)


class SystemOneStateProjector:
    """Render a self-contained provider frame without mutating task state."""

    @staticmethod
    def _question_specs(question_ids: tuple[str, ...],
                        route_candidates: tuple[str, ...]) -> tuple[QuestionSpec, ...]:
        result = []
        for question_id in question_ids:
            source = QUESTION_REGISTRY.get(question_id)
            if source is None:
                raise ValueError("unknown System-1 question")
            criteria = source["criteria"]
            if question_id == "route_preference":
                if not route_candidates:
                    raise ValueError("route preference requires legal route candidates")
                criteria = {name: "Already-authorized legal route." for name in route_candidates}
            result.append(QuestionSpec(
                question_id=question_id, family=str(source["family"]),
                question_type=str(source["type"]), instructions=str(source["instructions"]),
                criteria=tuple((str(key), str(value)) for key, value in criteria.items())))
        return tuple(result)

    def project(self, *, meta_state: MetacognitiveState,
                identity: SystemOneIdentity, sequence: int,
                policy_hash: str, policy: SystemOnePolicy,
                questions: tuple[str, ...], goal_contract: Mapping[str, object],
                convergence: Mapping[str, int], candidate: ActivityFact,
                phase: Optional[str] = None, active_segment: str = "segment-0",
                active_statework: Optional[str] = None, active_flow: Optional[str] = None,
                current_state: Optional[str] = None, blockers: Sequence[str] = (),
                failed_approaches: Sequence[AttemptSemanticDescriptor] = (),
                legal_routes: tuple[str, ...] = ()) -> SystemOneFrame:
        """Project already-owned host facts; never records or advances them."""
        task = {
            "objective": str(goal_contract.get("objective") or "")[:1024],
            "work_mode": str(goal_contract.get("work_mode") or "delivery"),
            "deliverable_kind": str(goal_contract.get("deliverable_kind") or "code_behavior"),
            "task_complexity": str(goal_contract.get("task_complexity") or "standard"),
            "phase": phase[:64] if phase else None,
            "active_segment": active_segment[:128] or "segment-0",
            "active_statework": active_statework[:64] if active_statework else None,
            "active_flow": active_flow[:64] if active_flow else None,
            "non_goals": tuple(str(item)[:256]
                                for item in list(goal_contract.get("non_goals") or ())[:8]),
        }
        states = {item.criterion_id: item for item in meta_state.criteria}
        criteria = []
        for item in list(goal_contract.get("acceptance_criteria") or ())[:8]:
            criterion_id = str(item.get("criterion_id") or "")[:128]
            criteria.append(states.get(criterion_id) or CriterionState(
                criterion_id=criterion_id,
                claim=str(item.get("claim") or "")[:512],
                kind=str(item.get("kind") or "")[:64]))
        workflows = tuple(sorted(
            meta_state.workflows,
            key=lambda item: (item.statework_id, item.subject_ref)))
        activity = meta_state.recent_activity
        convergence_values = {key: max(0, int(value)) for key, value in convergence.items()}
        situation = {
            "current_state": current_state[:128] if current_state else None,
            "blockers": tuple(
                str(item)[:256] for item in
                (list(blockers) if blockers else [
                    f"{item.blocker_id}: {item.summary}" if item.summary
                    else item.blocker_id for item in meta_state.blockers])[:8]),
            "actions_since_goal_progress": convergence_values.get(
                "total_actions_since_goal_progress", 0),
            "actions_since_artifact_change": meta_state.actions_since_artifact_change,
            "verification_actions_since_artifact_change": (
                meta_state.verification_actions_since_artifact_change),
            "recovery_epochs": convergence_values.get(
                "recovery_epochs_since_goal_progress", 0),
            "repeated_branch_count": convergence_values.get(
                "same_approach_retries", 0),
        }
        state = SemanticState(
            task=task, situation=situation, criteria=tuple(criteria), activity=activity,
            workflows=workflows, convergence=tuple(sorted(convergence_values.items())[:8]),
            candidate=candidate,
            failed_approaches=tuple(failed_approaches[-4:] or meta_state.failed_approaches),
            legal_routes=tuple(legal_routes[:32]))
        question_specs = self._question_specs(questions, legal_routes)
        state_digest = canonical_hash(state.to_dict())
        question_bundle_digest = canonical_hash([item.to_dict() for item in question_specs])
        request_id = f"system-one-{uuid.uuid4()}"
        request_digest = canonical_hash({
            "identity": identity.to_dict(), "sequence": sequence,
            "state_digest": state_digest,
            "question_bundle_digest": question_bundle_digest,
            "policy_hash": policy_hash,
        })
        return SystemOneFrame(
            identity=identity, request_id=request_id, sequence=sequence,
            state_digest=state_digest, question_bundle_digest=question_bundle_digest,
            request_digest=request_digest, policy_hash=policy_hash,
            backend_id=policy.backend_id, model_id=policy.model_id,
            model_revision=policy.model_revision, model_content_hash=policy.model_content_hash,
            provider_protocol_hash=policy.provider_protocol_hash,
            calibration_bundle_hash=policy.calibration_bundle_hash,
            state=state, questions=question_specs)


__all__ = ["SystemOneStateProjector"]
