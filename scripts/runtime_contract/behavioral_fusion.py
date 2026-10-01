"""Whitelisted behavioral guidance; it cannot grant runtime authority."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .actions import ActionDescriptor
from .system_one import QuestionAnswer, SystemOneJudgment
from .task import SystemOnePolicy

SUPPORT_RELATIONS = frozenset({"required_verification", "diagnostic_support", "unrelated"})
GOAL_RELATIONS = frozenset({"advances_primary_goal", "removes_concrete_blocker"})
BEHAVIORAL_FIELDS = frozenset({
    "focus_mode", "active_criterion_ids", "verification_intensity",
    "novelty_requirement", "goal_reanchor_required", "preferred_action_classes",
    "deprioritized_action_classes",
})


@dataclass(frozen=True)
class BehavioralDecision:
    """A behavioral preference, intentionally distinct from gate/authority results."""

    focus_mode: str = "delivery"
    active_criterion_ids: tuple[str, ...] = ()
    verification_intensity: str = "normal"
    novelty_requirement: str = "normal"
    goal_reanchor_required: bool = False
    preferred_action_classes: tuple[str, ...] = ()
    deprioritized_action_classes: tuple[str, ...] = ()
    source_decision_ids: tuple[str, ...] = ()
    source_state_digest: str = ""
    expires_after_sequence: Optional[int] = None

    def __post_init__(self) -> None:
        if self.focus_mode not in {"delivery", "diagnose", "verify", "recover", "replan"}:
            raise ValueError("unsupported behavioral focus mode")
        if self.verification_intensity not in {"minimal", "normal", "elevated"}:
            raise ValueError("unsupported verification intensity")
        if self.novelty_requirement not in {"normal", "changed_hypothesis", "fresh_evidence"}:
            raise ValueError("unsupported novelty requirement")
        if len(self.active_criterion_ids) > 8 or len(self.source_decision_ids) > 8:
            raise ValueError("behavioral decision exceeds bounded references")
        if len(self.preferred_action_classes) > 8 or len(self.deprioritized_action_classes) > 8:
            raise ValueError("behavioral action preference exceeds bounded options")

    def to_dict(self) -> dict[str, object]:
        return {
            "focus_mode": self.focus_mode,
            "active_criterion_ids": list(self.active_criterion_ids),
            "verification_intensity": self.verification_intensity,
            "novelty_requirement": self.novelty_requirement,
            "goal_reanchor_required": self.goal_reanchor_required,
            "preferred_action_classes": list(self.preferred_action_classes),
            "deprioritized_action_classes": list(self.deprioritized_action_classes),
            "source_decision_ids": list(self.source_decision_ids),
            "source_state_digest": self.source_state_digest,
            "expires_after_sequence": self.expires_after_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BehavioralDecision":
        allowed = {"focus_mode", "active_criterion_ids", "verification_intensity",
                   "novelty_requirement", "goal_reanchor_required",
                   "preferred_action_classes", "deprioritized_action_classes",
                   "source_decision_ids", "source_state_digest", "expires_after_sequence"}
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise ValueError("malformed behavioral control state")
        return cls(
            focus_mode=str(value["focus_mode"]),
            active_criterion_ids=tuple(value["active_criterion_ids"]),
            verification_intensity=str(value["verification_intensity"]),
            novelty_requirement=str(value["novelty_requirement"]),
            goal_reanchor_required=bool(value["goal_reanchor_required"]),
            preferred_action_classes=tuple(value["preferred_action_classes"]),
            deprioritized_action_classes=tuple(value["deprioritized_action_classes"]),
            source_decision_ids=tuple(value["source_decision_ids"]),
            source_state_digest=str(value["source_state_digest"]),
            expires_after_sequence=(int(value["expires_after_sequence"])
                                    if value["expires_after_sequence"] is not None else None))


@dataclass(frozen=True)
class AuthorityDecision:
    """Hard host decision; no System-1 object is accepted by its constructor."""

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
class ProgressFusion:
    progress_class: str
    criterion_id: Optional[str]
    host_corroborated: bool
    model_influenced: bool


def _answer(judgment: Optional[SystemOneJudgment], question_id: str,
            policy: SystemOnePolicy,
            calibrated: bool | Sequence[str]) -> Optional[QuestionAnswer]:
    allowed = calibrated if isinstance(calibrated, bool) else question_id in calibrated
    if judgment is None or policy.mode != "advisory" or not allowed:
        return None
    answer = judgment.answer(question_id)
    if (answer is None or answer.calibrated_probabilities is None
            or not answer.calibration_hash
            or answer.calibrated_confidence is None
            or answer.calibrated_confidence < policy.minimum_confidence):
        return None
    return answer


def fuse_action_relation(descriptor: ActionDescriptor, *,
                         judgment: Optional[SystemOneJudgment],
                         policy: SystemOnePolicy,
                         calibrated: bool | Sequence[str]) -> FusedActionRelation:
    """Host declaration wins; calibrated inference classifies only ambiguity."""
    if descriptor.goal_relation != "ambiguous":
        return FusedActionRelation(descriptor.goal_relation, "host_descriptor", False)
    answer = _answer(judgment, "action_relation", policy, calibrated)
    if answer is not None and answer.calibrated_confidence is not None:
        return FusedActionRelation(answer.selected, "calibrated_advisory", True,
                                   judgment.request_id)
    return FusedActionRelation("diagnostic_support", "conservative_host_default", False)


def fuse_progress_relation(*, host_progress_class: str, evidence_kinds: Sequence[str],
                           artifact_changed: Optional[bool],
                           criterion_id: Optional[str], criterion_status: Optional[str],
                           judgment: Optional[SystemOneJudgment],
                           policy: SystemOnePolicy,
                           calibrated: bool | Sequence[str]) -> ProgressFusion:
    """Use model for semantic classification; host facts alone establish goal progress."""
    answer = _answer(judgment, "progress_relation", policy, calibrated)
    selected = answer.selected if answer is not None else ""
    host_evidence_kinds = {
        "rendered_observation", "validator_result", "repository_evidence",
        "state_transition", "completion_boundary",
    }
    open_criterion = bool(
        criterion_id and criterion_status in {"unmet", "active", "partially_observed", "blocked"})
    artifact_corroborated = bool(
        open_criterion and artifact_changed is True
        and bool(host_evidence_kinds.intersection(evidence_kinds)))
    blocker_corroborated = bool(
        open_criterion and (artifact_corroborated or "state_transition" in evidence_kinds))
    if selected == "goal_progress" and artifact_corroborated:
        return ProgressFusion("GOAL_PROGRESS", criterion_id, True, True)
    if selected == "blocker_removed" and blocker_corroborated:
        return ProgressFusion("BLOCKER_REMOVED", criterion_id, True, True)
    host_classes = {"VERIFICATION_PROGRESS", "DIAGNOSTIC_PROGRESS", "RECOVERY_PROGRESS"}
    if host_progress_class in host_classes:
        return ProgressFusion(host_progress_class, criterion_id, False, False)
    mapping = {"verification_progress": "VERIFICATION_PROGRESS",
               "diagnostic_progress": "DIAGNOSTIC_PROGRESS",
               "recovery_progress": "RECOVERY_PROGRESS"}
    if selected in mapping:
        return ProgressFusion(mapping[selected], criterion_id, False, True)
    if selected == "goal_progress" and not artifact_corroborated:
        return ProgressFusion("DIAGNOSTIC_PROGRESS", criterion_id, False, False)
    if selected == "blocker_removed" and not blocker_corroborated:
        return ProgressFusion("DIAGNOSTIC_PROGRESS", criterion_id, False, False)
    return ProgressFusion("DIAGNOSTIC_PROGRESS", criterion_id, False, False)


def fuse_retry_relation(*, deterministic_same: bool,
                        judgment: Optional[SystemOneJudgment],
                        policy: SystemOnePolicy,
                        calibrated: bool | Sequence[str]) -> str:
    """Inference may collapse novelty but may never create it or reset budgets."""
    if deterministic_same:
        return "equivalent_retry"
    answer = _answer(judgment, "retry_relation", policy, calibrated)
    if answer is not None and answer.selected == "equivalent_retry":
        return "equivalent_retry"
    return "different_fingerprint"


class BehavioralFusionController:
    """Apply calibrated suggestions with two-vote hysteresis and short epochs."""

    def __init__(self, *, votes_required: int = 2) -> None:
        if votes_required < 1 or votes_required > 4:
            raise ValueError("behavior hysteresis votes must be between one and four")
        self.votes_required = votes_required
        self._last_vote = ""
        self._vote_count = 0
        self._epoch = 0
        self._state = BehavioralDecision()

    @property
    def state(self) -> BehavioralDecision:
        return self._state

    @property
    def epoch(self) -> int:
        return self._epoch

    def update(self, *, judgment: Optional[SystemOneJudgment], policy: SystemOnePolicy,
               calibrated: bool | Sequence[str], unmet_criterion_ids: Sequence[str],
               support_actions_since_progress: int, deterministic_reanchor: bool,
               sequence: int, support_action_limit: int = 5) -> BehavioralDecision:
        if support_action_limit < 1:
            raise ValueError("support_action_limit must be positive")
        active = tuple(unmet_criterion_ids[:8])
        action = _answer(judgment, "action_relation", policy, calibrated)
        progress = _answer(judgment, "progress_relation", policy, calibrated)
        retry = _answer(judgment, "retry_relation", policy, calibrated)
        drift = _answer(judgment, "goal_drift", policy, calibrated)
        votes = []
        if drift is not None:
            votes.append("replan" if drift.selected == "yes" else "delivery")
        elif progress is not None and progress.selected == "verification_progress":
            votes.append("verify")
        elif progress is not None and progress.selected == "recovery_progress":
            votes.append("recover")
        elif progress is not None and progress.selected == "diagnostic_progress":
            votes.append("diagnose")
        elif action is not None:
            votes.append("delivery" if action.selected in GOAL_RELATIONS else "diagnose")
        vote = votes[-1] if votes else ""
        if vote and vote == self._last_vote:
            self._vote_count += 1
        elif vote:
            self._last_vote, self._vote_count = vote, 1
        if vote and self._vote_count >= self.votes_required and vote != self._state.focus_mode:
            self._epoch += 1
            self._state = BehavioralDecision(
                focus_mode=vote, active_criterion_ids=active,
                verification_intensity=("elevated" if vote == "verify" else "normal"),
                novelty_requirement=("changed_hypothesis" if retry is not None
                                     and retry.selected == "equivalent_retry" else "normal"),
                goal_reanchor_required=(vote == "replan"),
                preferred_action_classes=("verify",) if vote == "verify" else (),
                deprioritized_action_classes=("unrelated",) if vote == "replan" else (),
                source_decision_ids=(judgment.request_id,) if judgment else (),
                source_state_digest=judgment.state_digest if judgment else "",
                expires_after_sequence=sequence + 4)
        elif not vote and deterministic_reanchor:
            self._epoch += 1
            self._state = BehavioralDecision(
                focus_mode="replan", active_criterion_ids=active,
                goal_reanchor_required=True, source_decision_ids=(),
                source_state_digest="", expires_after_sequence=sequence + 1)
        elif self._state.expires_after_sequence is not None and sequence > self._state.expires_after_sequence:
            self._epoch += 1
            self._state = BehavioralDecision(active_criterion_ids=active)
        if deterministic_reanchor or support_actions_since_progress >= support_action_limit:
            if not self._state.goal_reanchor_required:
                self._epoch += 1
                self._state = BehavioralDecision(
                    focus_mode="replan", active_criterion_ids=active,
                    goal_reanchor_required=True, source_decision_ids=(),
                    source_state_digest="", expires_after_sequence=sequence + 1)
        return self._state

    def snapshot(self) -> dict[str, object]:
        return {"state": self._state.to_dict(), "epoch": self._epoch,
                "last_vote": self._last_vote, "vote_count": self._vote_count,
                "votes_required": self.votes_required}

    def restore(self, value: Mapping[str, object]) -> None:
        expected = {"state", "epoch", "last_vote", "vote_count", "votes_required"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("malformed behavioral controller snapshot")
        if int(value["votes_required"]) != self.votes_required:
            raise ValueError("behavioral controller hysteresis changed across resume")
        epoch, votes = int(value["epoch"]), int(value["vote_count"])
        last_vote = str(value["last_vote"])
        if epoch < 0 or not 0 <= votes <= self.votes_required:
            raise ValueError("behavioral controller counters are out of bounds")
        if last_vote and last_vote not in {"delivery", "diagnose", "verify", "recover", "replan"}:
            raise ValueError("behavioral controller vote is unsupported")
        self._state = BehavioralDecision.from_dict(value["state"])
        self._epoch, self._last_vote, self._vote_count = epoch, last_vote, votes


def render_behavior_control_directive(state: BehavioralDecision,
                                      criterion_claims: Mapping[str, str]) -> str:
    """Render fixed host-authored instructions from bounded typed control state."""
    if not state.goal_reanchor_required and state.focus_mode == "delivery" and not state.preferred_action_classes:
        return ""
    lines = ["===== CURRENT EXECUTION FOCUS =====", f"Mode: {state.focus_mode}"]
    for criterion_id in state.active_criterion_ids[:4]:
        claim = str(criterion_claims.get(criterion_id) or "").replace("\n", " ").replace("\r", " ")[:160]
        lines.append(f"Active criterion {criterion_id}: {claim}" if claim
                     else f"Active criterion: {criterion_id}")
    if state.focus_mode == "replan":
        lines.append("Re-anchor the next action to an unfinished user criterion or its concrete blocker.")
        lines.append("Avoid unrelated investigation or scope expansion.")
    elif state.focus_mode == "verify":
        lines.append("Prefer verification that directly checks a changed artifact or active criterion.")
    elif state.focus_mode == "diagnose":
        lines.append("Gather only information needed to choose a criterion-directed next action.")
    elif state.focus_mode == "recover":
        lines.append("Use a fresh hypothesis and evidence while keeping host recovery limits binding.")
    if state.verification_intensity == "elevated":
        lines.append("Keep verification required where it proves the active criterion.")
    return "\n".join(lines[:8])


__all__ = ["BehavioralDecision", "AuthorityDecision", "BehavioralFusionController",
           "FusedActionRelation", "ProgressFusion", "fuse_action_relation",
           "fuse_progress_relation", "fuse_retry_relation",
           "render_behavior_control_directive", "SUPPORT_RELATIONS", "GOAL_RELATIONS",
           "BEHAVIORAL_FIELDS"]
