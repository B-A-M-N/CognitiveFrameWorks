"""Compose the effective runtime kernel for a resolved task."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping

from resolver.components import expand, order_kernel, requires_closure


@dataclass(frozen=True)
class TaskKernel:
    ordered: List[str]
    instructions: List[Dict[str, Any]]


def compose_task_kernel(
    *,
    profile: List[str],
    stages: Mapping[str, Any],
    aliases: Mapping[str, Any],
    required_components: List[str],
    enhanced_components: List[str],
    routing_adjustments: List[Dict[str, Any]],
) -> TaskKernel:
    """Apply stage preferences and normalized component requirements."""
    suppressed = {
        str(item.get("route")) for item in routing_adjustments
        if item.get("route_type") in {"stage", "lifecycle_stage"}
        and item.get("disposition") == "suppress"
    }
    selected = [stage for stage in profile if not any(
        stage == route or stage.split("_", 1)[0] == route
        for route in suppressed)]
    selected = requires_closure(selected, stages)
    kernel_stages = order_kernel(selected, stages)

    merged = list(kernel_stages)
    for requirement in [*required_components, *enhanced_components]:
        for concrete in expand([requirement], aliases):
            if concrete not in merged:
                merged.append(concrete)
    ordered = order_kernel(requires_closure(merged, stages), stages)

    preferred = {
        str(item.get("route")) for item in routing_adjustments
        if item.get("route_type") == "stage" and item.get("disposition") == "prefer"
    }
    instructions = [
        {
            "stage": stage_id,
            "routing_preference": "preferred" if stage_id in preferred else "default",
            "activation": stages[stage_id].get("activation_rule"),
            "outputs": stages[stage_id].get("output", []),
            "execution_mode": stages[stage_id].get("execution_mode"),
            "frequency": stages[stage_id].get("frequency"),
            "activation_predicates": stages[stage_id].get("activation_predicates", []),
        }
        for stage_id in ordered
    ]
    return TaskKernel(ordered, instructions)
