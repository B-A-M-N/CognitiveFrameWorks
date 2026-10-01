"""Narrow semantic fusion; metacognitive control remains provider-independent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .actions import ActionDescriptor
from .metacognition import MetaDecision, SemanticSignals
from .system_one import QuestionAnswer, SystemOneJudgment
from .task import SystemOnePolicy

SUPPORT_RELATIONS = frozenset({"required_verification", "diagnostic_support", "unrelated"})
GOAL_RELATIONS = frozenset({"advances_primary_goal", "removes_concrete_blocker"})


@dataclass(frozen=True)
class AuthorityDecision:
    """Hard host decision, independent of semantic inference."""

    outcome: str
    authority_source: str

    def __post_init__(self) -> None:
        if self.outcome not in {"ALLOW", "BLOCK", "REQUIRE_CONFIRMATION", "REQUIRE_REINSPECTION",
                                "REQUIRE_CHANGED_HYPOTHESIS", "REQUIRE_GOAL_REANCHOR",
                                "REQUIRE_RECOVERY"}:
            raise ValueError("unsupported authority outcome")
        if not self.authority_source:
            raise ValueError("authority decision requires a host source")


@dataclass(frozen=True)
class FusedActionRelation:
    relation: str
    source: str
    model_influenced: bool
    decision_id: Optional[str] = None


@dataclass(frozen=True)
class SemanticProgressObservation:
    """A meaning hint. It is deliberately incapable of resetting hard budgets."""

    relation: str
    criterion_id: Optional[str]
    model_influenced: bool
    decision_id: Optional[str] = None


def _answer(judgment: Optional[SystemOneJudgment], question_id: str,
            policy: SystemOnePolicy,
            calibrated: bool | Sequence[str]) -> Optional[QuestionAnswer]:
    allowed = calibrated if isinstance(calibrated, bool) else question_id in calibrated
    if judgment is None or policy.mode != "advisory" or not allowed:
        return None
    answer = judgment.answer(question_id)
    threshold = policy.confidence_threshold(question_id)
    if (answer is None or answer.calibrated_probabilities is None
            or not answer.calibration_hash
            or answer.calibrated_confidence is None
            or answer.calibrated_confidence < threshold):
        return None
    return answer


def fuse_action_relation(descriptor: ActionDescriptor, *,
                         judgment: Optional[SystemOneJudgment],
                         policy: SystemOnePolicy,
                         calibrated: bool | Sequence[str]) -> FusedActionRelation:
    """A trustworthy request-specific host relation wins; inference resolves ambiguity."""
    if descriptor.goal_relation != "ambiguous":
        return FusedActionRelation(descriptor.goal_relation, "host_descriptor", False)
    answer = _answer(judgment, "action_relation", policy, calibrated)
    if answer is not None:
        return FusedActionRelation(answer.selected, "calibrated_semantic_signal", True,
                                   judgment.request_id)
    return FusedActionRelation("diagnostic_support", "conservative_host_default", False)


def fuse_progress_relation(*, host_progress_class: str,
                           criterion_id: Optional[str],
                           judgment: Optional[SystemOneJudgment],
                           policy: SystemOnePolicy,
                           calibrated: bool | Sequence[str]) -> SemanticProgressObservation:
    """Interpret outcome semantics without asserting host progress or evidence validity."""
    answer = _answer(judgment, "progress_relation", policy, calibrated)
    if answer is not None:
        return SemanticProgressObservation(
            answer.selected, criterion_id, True, judgment.request_id)
    host_signal = {
        "GOAL_PROGRESS": "goal_progress",
        "BLOCKER_REMOVED": "blocker_removed",
        "VERIFICATION_PROGRESS": "verification_progress",
        "DIAGNOSTIC_PROGRESS": "diagnostic_progress",
        "RECOVERY_PROGRESS": "recovery_progress",
    }.get(host_progress_class, "no_progress")
    return SemanticProgressObservation(host_signal, criterion_id, False)


def fuse_retry_relation(*, deterministic_same: bool,
                        judgment: Optional[SystemOneJudgment],
                        policy: SystemOnePolicy,
                        calibrated: bool | Sequence[str]) -> str:
    """Semantic inference can collapse novelty but cannot manufacture a new branch."""
    if deterministic_same:
        return "equivalent_retry"
    answer = _answer(judgment, "retry_relation", policy, calibrated)
    if answer is not None and answer.selected == "equivalent_retry":
        return "equivalent_retry"
    return "different_fingerprint"


class SemanticFusion:
    """Stateless mapping from calibrated question answers to narrow signals."""

    @staticmethod
    def signals(judgment: Optional[SystemOneJudgment], *, policy: SystemOnePolicy,
                calibrated: bool | Sequence[str]) -> SemanticSignals:
        values: dict[str, object] = {}
        sources: list[str] = []
        for question_id in ("action_relation", "progress_relation", "retry_relation",
                            "goal_drift", "route_preference"):
            answer = _answer(judgment, question_id, policy, calibrated)
            if answer is None:
                continue
            selected = answer.selected
            values[question_id] = selected
            sources.append(judgment.request_id)
        retry = values.get("retry_relation")
        drift = values.get("goal_drift")
        return SemanticSignals(
            action_relation=(str(values["action_relation"])
                             if "action_relation" in values else None),
            progress_relation=(str(values["progress_relation"])
                               if "progress_relation" in values else None),
            equivalent_retry=(retry == "equivalent_retry" if retry is not None else None),
            possible_goal_drift=(drift == "yes" if drift is not None else None),
            route_preference=(str(values["route_preference"])
                              if "route_preference" in values else None),
            source_decisions=tuple(dict.fromkeys(sources))[:8])


def render_metacognitive_directive(decision: MetaDecision) -> str:
    """Render host-determined focus without giving provider labels executive force."""
    lines = ["===== CFW METACOGNITIVE GUIDANCE =====",
             f"Current focus: {decision.mode}",
             f"Reason: {decision.reason[:160]}"]
    if decision.active_criterion_ids:
        lines.append("Unfinished criteria: " + ", ".join(decision.active_criterion_ids[:4]))
    if decision.goal_reanchor_required:
        lines.append("Re-anchor the next action to an unfinished user criterion or a concrete blocker.")
    if decision.semantic_attention_note:
        lines.append(decision.semantic_attention_note[:220])
    if decision.mode == "BLOCKED":
        lines.append("Stop recovery attempts and report the unresolved blocker to the host.")
    return "\n".join(lines[:7])


__all__ = ["AuthorityDecision", "FusedActionRelation", "SemanticProgressObservation",
           "SemanticFusion", "fuse_action_relation", "fuse_progress_relation",
           "fuse_retry_relation", "render_metacognitive_directive", "SUPPORT_RELATIONS",
           "GOAL_RELATIONS"]
