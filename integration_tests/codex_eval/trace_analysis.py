"""Bounded descriptive comparisons for CFW per-turn behavior traces."""
from __future__ import annotations

import math
import hashlib
import json
import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any


MAX_TRACE_PAYLOAD_BYTES = 65_536


def bounded_payload_sha256(value: Any) -> str | None:
    """Fingerprint a bounded JSON payload without retaining its contents."""
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError):
        return None
    if len(encoded) > MAX_TRACE_PAYLOAD_BYTES:
        return None
    return hashlib.sha256(encoded).hexdigest()


def _distribution(values: Sequence[int | float]) -> dict[str, int | float | None]:
    selected = [float(value) for value in values
                if type(value) in {int, float} and math.isfinite(float(value))]
    if not selected:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(selected),
        "mean": round(statistics.fmean(selected), 3),
        "median": round(statistics.median(selected), 3),
        "min": round(min(selected), 3),
        "max": round(max(selected), 3),
    }


def anti_hyperfocus_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    """Measure bounded per-action attention and retry signals descriptively."""
    raw_actions = row.get("behavior_action_trace")
    actions = [item for item in raw_actions if isinstance(item, Mapping)] \
        if isinstance(raw_actions, list) else []
    raw_criteria = row.get("required_criterion_ids")
    if not isinstance(raw_criteria, (tuple, list)):
        results = row.get("criterion_results")
        raw_criteria = list(results) if isinstance(results, Mapping) else []
    criteria = sorted({value for value in raw_criteria
                       if isinstance(value, str) and value})
    if not criteria:
        criteria = sorted({value for item in actions
                           for value in item.get("criterion_ids", ())
                           if isinstance(value, str) and value})

    actions_per_criterion: Counter[str] = Counter()
    target_source_counts: Counter[str] = Counter()
    first_action: dict[str, int] = {}
    first_action_ms: dict[str, float] = {}
    target_sequence: list[str | None] = []
    action_times: list[int | float] = []
    same_approach_retries: int | None = None
    equivalent_retries: int | None = None
    equivalent_retry_proxy_count = 0
    local_yield_events: int | None = None
    global_blocked_events: int | None = None
    previous_signature: tuple[str, str] | None = None
    previous_progress = 0

    for index, item in enumerate(actions, start=1):
        target = item.get("target_criterion_id")
        if not isinstance(target, str) or target not in criteria:
            candidates = [value for value in item.get("criterion_ids", ())
                          if isinstance(value, str) and value in criteria]
            target = candidates[0] if len(candidates) == 1 else None
        target_sequence.append(target)
        source = item.get("target_source")
        target_source_counts[
            source if isinstance(source, str) and source else
            ("attributed" if target is not None else "unattributed")
        ] += 1
        if target is not None:
            actions_per_criterion[target] += 1
            first_action.setdefault(target, index)
            elapsed = item.get("elapsed_ms")
            if type(elapsed) in {int, float} and math.isfinite(float(elapsed)):
                first_action_ms.setdefault(target, round(float(elapsed), 3))
        elapsed = item.get("elapsed_ms")
        if type(elapsed) in {int, float} and math.isfinite(float(elapsed)):
            action_times.append(float(elapsed))
        same_count = item.get("same_approach_retry_count")
        equivalent_count = item.get("equivalent_retry_count")
        if type(same_count) is int and same_count >= 0:
            same_approach_retries = max(same_approach_retries or 0, same_count)
        if type(equivalent_count) is int and equivalent_count >= 0:
            equivalent_retries = max(equivalent_retries or 0, equivalent_count)
        action_class = item.get("action_class")
        signature = ((action_class if isinstance(action_class, str) else ""), target)
        progress = item.get("criterion_progress_delta")
        progress = progress if type(progress) is int else 0
        if (signature[0] and target is not None and signature == previous_signature
                and previous_progress == 0 and progress == 0):
            equivalent_retry_proxy_count += 1
        previous_signature = signature if signature[0] and target else None
        previous_progress = progress

    max_streak = 0
    max_streak_criterion: str | None = None
    current_streak = 0
    current_target: str | None = None
    rotations = 0
    previous_target: str | None = None
    ages = {criterion_id: 0 for criterion_id in criteria}
    max_starvation_age = 0
    for target in target_sequence:
        if target is not None:
            if target == current_target:
                current_streak += 1
            else:
                current_streak = 1
                current_target = target
            if current_streak > max_streak:
                max_streak = current_streak
                max_streak_criterion = target
            if previous_target is not None and target != previous_target:
                rotations += 1
            previous_target = target
        else:
            current_streak = 0
            current_target = None
        for criterion_id in criteria:
            if criterion_id == target:
                ages[criterion_id] = 0
            else:
                ages[criterion_id] += 1
                max_starvation_age = max(max_starvation_age, ages[criterion_id])

    turn_trace = row.get("behavior_trace")
    turns = [item for item in turn_trace if isinstance(item, Mapping)] \
        if isinstance(turn_trace, list) else []
    if any(type(item.get("local_yield_event")) is bool for item in turns):
        local_yield_events = sum(item.get("local_yield_event") is True for item in turns)
    if any(type(item.get("global_blocked_event")) is bool for item in turns):
        global_blocked_events = sum(
            item.get("global_blocked_event") is True for item in turns)

    return {
        "required_criterion_count": len(criteria),
        "action_count": len(actions),
        "unattributed_action_count": sum(
            target is None for target in target_sequence),
        "criterion_attribution_ratio": (
            round(sum(target is not None for target in target_sequence) / len(actions), 3)
            if actions else None),
        "action_target_source_counts": dict(sorted(target_source_counts.items())),
        "actions_per_criterion": {
            criterion_id: actions_per_criterion.get(criterion_id, 0)
            for criterion_id in criteria},
        "maximum_consecutive_actions_on_one_criterion": max_streak,
        "maximum_consecutive_actions_criterion": max_streak_criterion,
        "maximum_criterion_starvation_age_actions": (
            max_starvation_age if actions else None),
        "time_to_first_action_ms_by_criterion": {
            criterion_id: first_action_ms.get(criterion_id)
            for criterion_id in criteria},
        "first_action_index_by_criterion": {
            criterion_id: first_action.get(criterion_id)
            for criterion_id in criteria},
        "criteria_never_touched": [
            criterion_id for criterion_id in criteria
            if actions_per_criterion.get(criterion_id, 0) == 0],
        "attention_rotations": rotations,
        "same_approach_retries": same_approach_retries,
        "equivalent_retries": equivalent_retries,
        "equivalent_retry_proxy_count": equivalent_retry_proxy_count,
        "local_yield_events": local_yield_events,
        "global_blocked_events": global_blocked_events,
        "elapsed_to_last_action_ms": (
            round(float(action_times[-1]), 3) if action_times else None),
        "starvation_age_interpretation": (
            "upper_bound_when_actions_are_unattributed; unattributed actions count as elapsed"
            if any(target is None for target in target_sequence)
            else "all recorded actions have a criterion target"),
    }


def _trace_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    trace = row.get("behavior_trace")
    turns = [item for item in trace if isinstance(item, Mapping)] \
        if isinstance(trace, list) else []
    mode_counts: Counter[str] = Counter()
    transition_counts: Counter[str] = Counter()
    focus_counts: Counter[str] = Counter()
    component_mode_counts: Counter[str] = Counter()
    validator_orders: Counter[str] = Counter()
    action_calls = writes = completion_attempts = redirects = 0
    retries: int | None = None
    guidance_sizes: list[int] = []
    identical_guidance = attention_rotations = 0
    previous_digest: str | None = None
    previous_focus: tuple[str, ...] | None = None
    for item in turns:
        mode = item.get("behavioral_mode")
        if isinstance(mode, str):
            mode_counts[mode] += 1
        transition = item.get("mode_transition")
        if isinstance(transition, str) and transition != "initial":
            transition_counts[transition] += 1
        focus_value = item.get("focus_criterion_ids")
        focus = tuple(value for value in focus_value if isinstance(value, str)) \
            if isinstance(focus_value, list) else ()
        for criterion_id in focus:
            focus_counts[criterion_id] += 1
        raw_component_modes = item.get("component_modes")
        if isinstance(raw_component_modes, Mapping):
            for component, component_mode in list(raw_component_modes.items())[:16]:
                if (isinstance(component, str)
                        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", component)
                        and isinstance(component_mode, str)
                        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", component_mode)):
                    component_mode_counts[f"{component}:{component_mode}"] += 1
        if previous_focus is not None and focus != previous_focus:
            attention_rotations += 1
        previous_focus = focus
        digest = item.get("guidance_sha256")
        if isinstance(digest, str) and digest == previous_digest:
            identical_guidance += 1
        previous_digest = digest if isinstance(digest, str) else None
        size = item.get("guidance_byte_count")
        if type(size) is int and size >= 0:
            guidance_sizes.append(size)
        for field, accumulator in (
                ("model_tool_call_count", "actions"),
                ("native_write_request_count", "writes"),
                ("completion_attempt_count", "attempts")):
            value = item.get(field)
            if type(value) is int and value >= 0:
                if accumulator == "actions":
                    action_calls += value
                elif accumulator == "writes":
                    writes += value
                else:
                    completion_attempts += value
        redirects += item.get("redirected") is True
        counters = item.get("retry_recovery_counters")
        if isinstance(counters, Mapping):
            value = counters.get("same_approach_retries")
            if type(value) is int and value >= 0:
                retries = max(retries or 0, value)
        order = item.get("completion_validator_order")
        if isinstance(order, list):
            safe_order = [value for value in order if isinstance(value, str)]
            if safe_order:
                validator_orders[" > ".join(safe_order)] += 1
    return {
        "trial_id": (str(row.get("trial_id"))[:128]
                     if isinstance(row.get("trial_id"), str) else ""),
        "turn_count": len(turns),
        "model_tool_call_count": action_calls,
        "native_write_request_count": writes,
        "completion_attempt_count": completion_attempts,
        "redirect_turn_count": redirects,
        "max_same_approach_retries": retries,
        "identical_guidance_adjacent_pairs": identical_guidance,
        "attention_rotation_count": attention_rotations,
        "guidance_byte_count_mean": (
            round(statistics.fmean(guidance_sizes), 3) if guidance_sizes else None),
        "focus_criterion_turn_counts": dict(sorted(focus_counts.items())),
        "component_mode_turn_counts": dict(sorted(component_mode_counts.items())),
        "behavioral_mode_counts": dict(sorted(mode_counts.items())),
        "mode_transition_counts": dict(sorted(transition_counts.items())),
        "completion_validator_order_counts": dict(sorted(validator_orders.items())),
        "anti_hyperfocus": anti_hyperfocus_metrics(row),
    }


def compare_behavior_trace_outcomes(trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare traces by fixture outcome without claiming causal identification."""
    grouped: dict[str, list[dict[str, Any]]] = {
        "complete_4_of_4": [], "below_4_of_4": []}
    for row in trials:
        score = row.get("criteria_passed")
        if type(score) is int:
            name = "complete_4_of_4" if score == 4 else "below_4_of_4"
            grouped[name].append(_trace_metrics(row))

    cohorts: dict[str, dict[str, Any]] = {}
    for name, rows in grouped.items():
        action_metrics = [row["anti_hyperfocus"] for row in rows]
        criterion_counts = sum_counters(
            row["anti_hyperfocus"]["actions_per_criterion"] for row in rows)
        touched_criteria = sorted(criterion_counts)
        cohorts[name] = {
            "trials": len(rows),
            "turn_count_distribution": _distribution(
                [row["turn_count"] for row in rows]),
            "model_tool_call_distribution": _distribution(
                [row["model_tool_call_count"] for row in rows]),
            "native_write_request_distribution": _distribution(
                [row["native_write_request_count"] for row in rows]),
            "completion_attempt_distribution": _distribution(
                [row["completion_attempt_count"] for row in rows]),
            "redirect_turn_distribution": _distribution(
                [row["redirect_turn_count"] for row in rows]),
            "same_approach_retry_distribution": _distribution(
                [row["max_same_approach_retries"] for row in rows]),
            "identical_guidance_pair_distribution": _distribution(
                [row["identical_guidance_adjacent_pairs"] for row in rows]),
            "attention_rotation_distribution": _distribution(
                [row["attention_rotation_count"] for row in rows]),
            "guidance_byte_count_mean_distribution": _distribution(
                [row["guidance_byte_count_mean"] for row in rows
                 if row["guidance_byte_count_mean"] is not None]),
            "focus_criterion_turn_counts": dict(sorted(sum_counters(
                row["focus_criterion_turn_counts"] for row in rows).items())),
            "component_mode_turn_counts": dict(sorted(sum_counters(
                row["component_mode_turn_counts"] for row in rows).items())),
            "behavioral_mode_counts": dict(sorted(sum_counters(
                row["behavioral_mode_counts"] for row in rows).items())),
            "mode_transition_counts": dict(sorted(sum_counters(
                row["mode_transition_counts"] for row in rows).items())),
            "completion_validator_order_counts": dict(sorted(sum_counters(
                row["completion_validator_order_counts"] for row in rows).items())),
            "anti_hyperfocus": {
                "trials_with_action_trace": sum(
                    row["anti_hyperfocus"]["action_count"] > 0 for row in rows),
                "actions_per_criterion": dict(sorted(criterion_counts.items())),
                "criterion_attribution_ratio_distribution": _distribution([
                    row["criterion_attribution_ratio"] for row in action_metrics
                    if row["criterion_attribution_ratio"] is not None]),
                "unattributed_action_count_distribution": _distribution([
                    row["unattributed_action_count"] for row in action_metrics
                    if row["action_count"] > 0]),
                "action_target_source_counts": dict(sorted(sum_counters(
                    row["action_target_source_counts"] for row in action_metrics).items())),
                "trial_metrics": [
                    {"trial_id": row.get("trial_id"), **row["anti_hyperfocus"]}
                    for row in rows],
                "maximum_consecutive_actions_distribution": _distribution([
                    row["maximum_consecutive_actions_on_one_criterion"]
                    for row in action_metrics
                    if row["action_count"] > 0]),
                "maximum_starvation_age_actions_distribution": _distribution([
                    row["maximum_criterion_starvation_age_actions"]
                    for row in action_metrics
                    if type(row["maximum_criterion_starvation_age_actions"]) is int]),
                "attention_rotation_distribution": _distribution([
                    row["attention_rotations"]
                    for row in action_metrics if row["action_count"] > 0]),
                "same_approach_retry_distribution": _distribution([
                    row["same_approach_retries"]
                    for row in action_metrics if row["action_count"] > 0]),
                "equivalent_retry_distribution": _distribution([
                    row["equivalent_retries"]
                    for row in action_metrics if row["action_count"] > 0]),
                "equivalent_retry_proxy_distribution": _distribution([
                    row["equivalent_retry_proxy_count"]
                    for row in action_metrics if row["action_count"] > 0]),
                "local_yield_event_distribution": _distribution([
                    row["local_yield_events"]
                    for row in action_metrics
                    if type(row["local_yield_events"]) is int]),
                "global_blocked_event_distribution": _distribution([
                    row["global_blocked_events"]
                    for row in action_metrics
                    if type(row["global_blocked_events"]) is int]),
                "time_to_first_action_ms_by_criterion": {
                    criterion_id: _distribution([
                        row["time_to_first_action_ms_by_criterion"].get(criterion_id)
                        for row in action_metrics
                        if type(row["time_to_first_action_ms_by_criterion"].get(
                                criterion_id)) in {int, float}])
                    for criterion_id in touched_criteria},
                "criteria_never_touched_trials": sum(
                    bool(row["criteria_never_touched"])
                    for row in action_metrics),
                "interpretation": (
                    "descriptive action-level signals; missing traces remain unavailable"),
            },
        }

    metrics = (
        "model_tool_call_count", "native_write_request_count",
        "completion_attempt_count", "redirect_turn_count",
        "max_same_approach_retries", "identical_guidance_adjacent_pairs",
        "attention_rotation_count",
    )
    differences = {}
    for metric in metrics:
        complete = [row[metric] for row in grouped["complete_4_of_4"]
                    if type(row[metric]) in {int, float}]
        below = [row[metric] for row in grouped["below_4_of_4"]
                 if type(row[metric]) in {int, float}]
        complete_mean = (statistics.fmean(complete) if complete else None)
        below_mean = (statistics.fmean(below) if below else None)
        differences[metric] = (
            round(complete_mean - below_mean, 3)
            if complete_mean is not None and below_mean is not None else None)
    return {
        "interpretation": "descriptive outcome association; this comparison does not identify causation",
        "cohorts": cohorts,
        "complete_minus_below_mean_differences": differences,
    }


def sum_counters(values: Sequence[Mapping[str, int]]) -> Counter[str]:
    result: Counter[str] = Counter()
    for value in values:
        result.update({key: count for key, count in value.items()
                       if isinstance(key, str) and type(count) is int and count >= 0})
    return result


__all__ = ["anti_hyperfocus_metrics", "bounded_payload_sha256",
           "compare_behavior_trace_outcomes"]
