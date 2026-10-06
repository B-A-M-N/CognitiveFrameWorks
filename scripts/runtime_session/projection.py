"""Model-visible context projection for a CFW runtime session."""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from runtime_contract.component_control import COMPONENT_MODEL_VISIBILITY
from runtime_contract.handlers import enforcement_for_guard
from runtime_contract.task import task_goal_contract_mapping_hash
from runtime_contract.behavioral_fusion import render_metacognitive_directive

def context_for(session: Any, *, behavior_context: str = "",
                active_statework_ids: Optional[Sequence[str]] = None) -> str:
    """The model-visible instruction context: kernel order + injected
    capsules + guard rules. This is what measuring 'the final prompt
    actually injected' means — not the bundle JSON file."""
    parts: List[str] = []
    if active_statework_ids is not None:
        if (not isinstance(active_statework_ids, (tuple, list, set, frozenset))
                or len(active_statework_ids) > 32
                or any(not isinstance(item, str) or not item or len(item) > 128
                       for item in active_statework_ids)
                or len(active_statework_ids) != len(set(active_statework_ids))):
            raise ValueError("external active StateWork IDs are malformed")
        available_stateworks = {str(item.get("id") or "")
                                for item in session.bundle.stateworks}
        if (not session.behavior.is_external
                and not set(active_statework_ids).issubset(available_stateworks)):
            raise ValueError("external active StateWork IDs escape the pinned bundle")
    goal = session.bundle.pinned.get("goal_contract") or {}
    if goal:
        criteria = list(goal.get("acceptance_criteria") or ())
        required = [item for item in criteria if item.get("required", True)]
        required_count = len(required)
        if session.behavior.is_external:
            effective_state = session.effective_external_behavior_projection()
            focus = set(effective_state.get("focus_criteria") or ())
            visible = [item for item in required
                       if str(item.get("criterion_id") or "") in focus]
            task_set_line = (
                f"Complete required criterion set: {len(required)} items. "
                "The host tracks every item; details below show only the current DP attention window.\n")
            required = visible
            criteria_label = "Required criteria in current attention window: "
        else:
            attention = session._evaluate_metacognition()
            focus = set(attention.active_criterion_ids)
            required = [item for item in required
                        if str(item.get("criterion_id") or "") in focus]
            task_set_line = (
                f"Complete required criterion set: {required_count} items. "
                "The host tracks every item; details below show only the current standalone attention window.\n")
            criteria_label = "Required criteria in current attention window: "
        parts.append(
            "===== TASK CONTRACT =====\n"
            f"Primary outcome: {goal.get('objective', '')}\n"
            f"Deliverable: {goal.get('deliverable_kind', 'unspecified')}; "
            f"work mode: {goal.get('work_mode', 'delivery')}\n"
            + task_set_line
            + criteria_label + ("; ".join(
                f"{item.get('criterion_id')}: {item.get('claim')}" for item in required
            ) or "none specified") + "\n"
            + "Support work is subordinate to these criteria.\n"
            )
        non_goals = list(goal.get("non_goals") or ())
        if non_goals:
            parts.append("Non-goals: " + "; ".join(str(item) for item in non_goals))
    else:
        parts.append(
            "===== TASK CONTRACT =====\n"
            "The host did not supply a structured task contract. Preserve the "
            "user's requested outcome; framework workflow is support work.")
    pinned_goal_hash = str(session.bundle.pinned.get("goal_contract_hash") or "")
    goal_anchor_valid = (
        goal.get("work_mode") != "delivery"
        or bool(goal and pinned_goal_hash
                and task_goal_contract_mapping_hash(goal) == pinned_goal_hash))
    if (not session.behavior.is_external and goal.get("work_mode") == "delivery"
            and not goal_anchor_valid):
        parts.append(
            "===== GOAL REANCHOR REQUIRED =====\n"
            "The host task contract is missing or its pinned identity changed. "
            "A trusted host must restore the authoritative objective and required criteria.")
    elif (not session.behavior.is_external and goal_anchor_valid
            and goal.get("work_mode") == "delivery"
            and session.behavior.metacognitive.take_progress_redirect_guidance() is not None):
        convergence_state = (session.attempts.controller.state("primary-objective")
                             if session.attempts.controller is not None else None)
        total_actions = (convergence_state.total_actions_since_goal_progress
                         if convergence_state else 0)
        support_actions = (convergence_state.support_actions_since_goal_progress
                           if convergence_state else 0)
        focus = session.behavior.metacognitive.active_attention_window()
        parts.append(
            "===== PROGRESS REDIRECT REQUIRED =====\n"
            f"No primary-deliverable progress in {total_actions} committed actions; "
            f"{support_actions} were host-classified support. "
            f"Primary outcome: {goal.get('objective', '')}\n"
            "The existing host contract remains authoritative. The host has yielded attention after this bounded redirect. "
            "Continue with an unfinished criterion, establish a concrete blocker, or verify criterion-linked work already performed. "
            "Current focus: " + (", ".join(focus) if focus else "no runnable criterion"))
    directive = ("" if session.behavior.is_external else
                 render_metacognitive_directive(session._evaluate_metacognition()))
    if directive:
        parts.append(directive +
                     "\nHost permissions, evidence requirements, retry limits, and completion checks remain authoritative.")
    active_stages = [stage for stage in session.bundle.kernel
                     if stage in session.lifecycle_workflow.active_lifecycle]
    if not active_stages and not (session.behavior.is_external
                                  and session.behavior.external_projection):
        active_stages = list(session.bundle.kernel[:1])
    visible_stages = [
        stage for stage in active_stages
        if COMPONENT_MODEL_VISIBILITY.get(
            str(session.lifecycle_workflow.lifecycle.specs.get(stage, {}).get("owner") or stage).lower(),
            "none") == "guidance"
    ]
    # Only guidance-visible stages belong in the prompt's pipeline summary.
    # A component being active for host-side enforcement does not itself
    # grant permission to disclose its identity or internal state to the model.
    if not session.behavior.is_external and visible_stages:
        parts.append("ACTIVE PIPELINE: " + " -> ".join(visible_stages))
    seen_capsules: set[tuple[str, str]] = set()
    for stage_id in visible_stages:
        capsule = session.injected.get(stage_id)
        if capsule:
            owner = stage_id.split("_")[0]
            identity = (owner, capsule)
            if identity in seen_capsules:
                continue
            seen_capsules.add(identity)
            parts.append(f"\n===== {stage_id.upper()} =====\n{capsule}")
    filter_stateworks = active_statework_ids is not None or bool(
        session.lifecycle_workflow.active_statework_ids)
    active_ids = (session.lifecycle_workflow.active_statework_ids if active_statework_ids is None
                  else set(active_statework_ids))
    for key, capsule in session.injected.items():
        if not key.startswith("statework"):
            continue
        if filter_stateworks and not any(
                key.startswith(f"statework:{sid}")
                or key.startswith(f"statework-flow:{sid}:")
                or key.startswith(f"statework-specialist:{sid}:")
                for sid in active_ids):
            continue
        parts.append(f"\n===== {key.upper()} =====\n{capsule}")
    guards = session.bundle.guard_pack.get("guards", [])
    prompt_guards = [g for g in guards
                     if enforcement_for_guard(g).get("mode") == "prompt"]
    if prompt_guards:
        parts.append("\n===== GUARD RULES =====\n" +
                     "\n".join(f"- {g['rule']}" for g in prompt_guards))
    handoff = session.bundle.handoff_plan
    if handoff:
        # Only the current segment's handoff is model-visible.  A complete
        # prerequisite chain would reveal future StateWorks in the same
        # prompt, defeating exclusive segment activation.
        active_handoff = handoff[min(session.lifecycle_workflow.current_segment_index, len(handoff) - 1)]
        parts.append("\n===== ACTIVE HANDOFF =====\n" +
                     f"- {active_handoff['producer']} -> {active_handoff['packet']}")
    if session.attempts.controller is not None and not session.behavior.is_external:
        blocks = [state.compact_state_block(
            max_recovery_epochs_without_progress=session.attempts.controller.max_recovery_epochs_without_progress)
                  for state in session.attempts.controller.states()]
        blocks = [block for block in blocks if block]
        if blocks:
            parts.append("\n===== CONVERGENCE =====\n" + "\n\n".join(blocks))
    action_hint = ("" if session.behavior.is_external
                   else session._action_strategy_hint())
    if action_hint:
        parts.append(action_hint)
    if behavior_context:
        if not isinstance(behavior_context, str) or len(behavior_context) > 32_768:
            raise ValueError("external behavioral context must be bounded text")
        parts.append(behavior_context)
    return "\n".join(parts)
