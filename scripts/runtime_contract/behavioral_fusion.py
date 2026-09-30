"""Single CFW-owned location for advisory System-1 fusion."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .actions import ActionDescriptor
from .system_one import SystemOneJudgment, SystemOnePolicy


SUPPORT_RELATIONS = frozenset({"required_verification", "diagnostic_support", "unrelated"})
GOAL_RELATIONS = frozenset({"advances_primary_goal", "removes_concrete_blocker"})


@dataclass(frozen=True)
class FusedActionRelation:
    relation: str
    source: str
    model_influenced: bool


def fuse_action_relation(descriptor: ActionDescriptor, *,
                         judgment: Optional[SystemOneJudgment],
                         policy: SystemOnePolicy, calibrated: bool) -> FusedActionRelation:
    """Use declared host semantics first; model may annotate only calibrated advisory.

    This classification never changes WARD, completion, transition, or retry
    authority. Shadow output is observable but has zero influence.
    """
    if descriptor.goal_relation != "ambiguous":
        return FusedActionRelation(descriptor.goal_relation, "host_descriptor", False)
    if (policy.mode == "advisory" and calibrated and judgment is not None
            and judgment.confidence >= policy.minimum_confidence
            and judgment.action_relation is not None):
        return FusedActionRelation(judgment.action_relation, "calibrated_advisory", True)
    return FusedActionRelation("diagnostic_support", "conservative_host_default", False)


def advisory_guidance(*, judgment: Optional[SystemOneJudgment],
                      policy: SystemOnePolicy, calibrated: bool) -> tuple[str, ...]:
    """Render only fixed host text from calibrated labels; never free-form output."""
    if (policy.mode != "advisory" or not calibrated or judgment is None
            or judgment.confidence < policy.minimum_confidence):
        return ()
    guidance = []
    if judgment.action_relation is not None:
        guidance.append(
            f"Candidate action relation: {judgment.action_relation}; advisory classification only.")
    if judgment.progress_relation in {"goal_progress", "blocker_removed"}:
        guidance.append(
            "Possible goal progress; require host-attested evidence matched to a pinned criterion.")
    elif judgment.progress_relation is not None:
        guidance.append(f"Candidate progress relation: {judgment.progress_relation}; advisory only.")
    if judgment.retry_relation is not None:
        guidance.append(
            f"Candidate retry relation: {judgment.retry_relation}; deterministic retry limits remain binding.")
    if judgment.goal_drift == "yes":
        guidance.append("Possible goal drift; compare the next action with pinned acceptance criteria.")
    if judgment.route_preference is not None:
        guidance.append(
            f"Eligible route preference: {judgment.route_preference}; static host eligibility remains binding.")
    return tuple(guidance[:4])


__all__ = ["FusedActionRelation", "fuse_action_relation", "advisory_guidance",
           "SUPPORT_RELATIONS", "GOAL_RELATIONS"]
