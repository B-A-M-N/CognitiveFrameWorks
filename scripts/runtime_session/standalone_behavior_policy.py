"""Small deterministic behavior policy used only when CFW owns the session."""
from __future__ import annotations

from dataclasses import dataclass

from runtime_contract.task_evidence import CompletionReadiness


@dataclass(frozen=True)
class StandaloneBehaviorFacts:
    """Minimal CFW host facts consumed by the deterministic fallback."""

    active_criterion_ids: tuple[str, ...] = ()
    has_active_blockers: bool = False
    pending_verification_obligations: int = 0
    progress_redirect_guidance_pending: bool = False
    progress_redirected: bool = False

    def __post_init__(self) -> None:
        criteria = tuple(self.active_criterion_ids)
        if (len(criteria) > 8 or len(criteria) != len(set(criteria))
                or any(not isinstance(item, str) or not item or len(item) > 128
                       for item in criteria)):
            raise ValueError("standalone behavior facts contain malformed criterion IDs")
        object.__setattr__(self, "active_criterion_ids", criteria)
        if type(self.has_active_blockers) is not bool:
            raise ValueError("standalone blocker fact must be boolean")
        if (type(self.pending_verification_obligations) is not int
                or self.pending_verification_obligations < 0):
            raise ValueError("pending verification obligation count must be non-negative")
        if (type(self.progress_redirect_guidance_pending) is not bool
                or type(self.progress_redirected) is not bool):
            raise ValueError("standalone redirect facts must be boolean")


@dataclass(frozen=True)
class StandaloneBehaviorDecision:
    """CFW-only fallback decision; it carries no semantic-provider authority."""

    mode: str
    reason: str
    active_criterion_ids: tuple[str, ...] = ()
    goal_reanchor_required: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"DELIVER", "DIAGNOSE", "VERIFY", "RECOVER", "REPLAN",
                             "BLOCKED", "REINSPECT", "READY_TO_COMPLETE"}:
            raise ValueError("unsupported standalone behavior decision")
        if len(self.active_criterion_ids) > 8:
            raise ValueError("standalone behavior decision exceeds focus bound")
        if any(not isinstance(item, str) or not item or len(item) > 128
               for item in self.active_criterion_ids):
            raise ValueError("standalone behavior focus IDs must be bounded text")
        if not isinstance(self.goal_reanchor_required, bool):
            raise ValueError("goal reanchor flag must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {"mode": self.mode, "reason": self.reason,
                "active_criterion_ids": list(self.active_criterion_ids),
                "goal_reanchor_required": self.goal_reanchor_required}


class StandaloneBehaviorPolicy:
    """Choose a CFW-only fallback mode from host facts and bounded budgets.

    This policy has no semantic-provider input, learned profile, or authority
    over CFW's local action and completion gates. Combined deployments use DP
    for global behavior selection and do not instantiate this policy.
    """

    def decide(self, facts: StandaloneBehaviorFacts, *,
               support_actions_since_progress: int,
               support_action_limit: int,
               total_actions_since_progress: int,
               total_action_limit: int,
               recovery_epochs_since_progress: int,
               max_recovery_epochs: int,
               same_approach_retries: int,
               max_same_approach_retries: int,
               unresolved_contradiction: bool = False,
               goal_contract_anchor_valid: bool = True,
               completion_readiness: CompletionReadiness | None = None
               ) -> StandaloneBehaviorDecision:
        if not isinstance(facts, StandaloneBehaviorFacts):
            raise TypeError("standalone policy requires bounded host behavior facts")
        active = facts.active_criterion_ids
        if unresolved_contradiction:
            return StandaloneBehaviorDecision(
                "REINSPECT", "unresolved operator contradiction", active)
        if not goal_contract_anchor_valid:
            return StandaloneBehaviorDecision(
                "REPLAN", "host goal contract is missing or no longer matches its pinned identity",
                active, goal_reanchor_required=True)
        if recovery_epochs_since_progress >= max_recovery_epochs:
            if active:
                return StandaloneBehaviorDecision(
                    "REPLAN", "current recovery budget exhausted; yield to runnable criteria",
                    active)
            return StandaloneBehaviorDecision("BLOCKED", "hard recovery budget exhausted", active)
        if support_actions_since_progress >= support_action_limit:
            if facts.progress_redirect_guidance_pending or not facts.progress_redirected:
                return StandaloneBehaviorDecision(
                    "REPLAN", "support-action budget reached; redirect to an unfinished criterion",
                    active)
            return StandaloneBehaviorDecision(
                "DELIVER", "progress redirect issued; continue with a runnable criterion",
                active)
        if total_actions_since_progress >= total_action_limit:
            if facts.progress_redirect_guidance_pending or not facts.progress_redirected:
                return StandaloneBehaviorDecision(
                    "REPLAN", "committed-action budget reached without goal evidence",
                    active, goal_reanchor_required=True)
            return StandaloneBehaviorDecision(
                "DELIVER", "progress redirect issued; continue with a runnable criterion",
                active)
        if same_approach_retries >= max_same_approach_retries:
            return StandaloneBehaviorDecision(
                "RECOVER", "repeated approach reached its hard limit", active)
        if facts.has_active_blockers:
            return StandaloneBehaviorDecision(
                "DIAGNOSE", "host-observed blocker remains", active)
        if facts.pending_verification_obligations:
            return StandaloneBehaviorDecision(
                "VERIFY", "explicit criterion verification obligation is pending", active)
        if active:
            return StandaloneBehaviorDecision(
                "DELIVER", "required criterion remains unfinished", active)
        if completion_readiness is not None and completion_readiness.ready:
            return StandaloneBehaviorDecision(
                "READY_TO_COMPLETE", "host completion prerequisites are ready")
        if (completion_readiness is not None
                and not completion_readiness.active_stateworks_complete):
            return StandaloneBehaviorDecision(
                "DELIVER", "active workflow still has required steps")
        return StandaloneBehaviorDecision(
            "VERIFY", "completion readiness still requires host evidence")


def render_standalone_behavior_guidance(
        decision: StandaloneBehaviorDecision) -> str:
    """Render deterministic fallback guidance from the standalone decision."""
    lines = ["===== CFW STANDALONE BEHAVIOR =====",
             f"Current focus: {decision.mode}",
             f"Reason: {decision.reason[:160]}"]
    if decision.active_criterion_ids:
        lines.append("Unfinished criteria: "
                     + ", ".join(decision.active_criterion_ids[:4]))
    if decision.goal_reanchor_required:
        lines.append(
            "Re-anchor the next action to an unfinished user criterion or a concrete blocker.")
    if decision.mode == "BLOCKED":
        lines.append("Stop recovery attempts and report the unresolved blocker to the host.")
    elif decision.mode == "READY_TO_COMPLETE":
        lines.append("Invoke the host's authoritative completion and boundary-evidence checks.")
    return "\n".join(lines[:7])


__all__ = ["StandaloneBehaviorDecision", "StandaloneBehaviorFacts",
           "StandaloneBehaviorPolicy",
           "render_standalone_behavior_guidance"]
