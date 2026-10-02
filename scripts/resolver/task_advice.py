"""Validate advisory routing preferences against host-owned choices."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from runtime_contract.advisor import (
    BehavioralAdvisor, bounded_adjustments, validate_advice_transaction,
)
from runtime_contract.routing import (
    comparison_context_hash, validate_adaptive_adjustment,
)
from resolver.guards import load_guard_pack


@dataclass(frozen=True)
class TaskAdviceResolution:
    routing_context: Dict[str, Any]
    routing_adjustments: List[Dict[str, Any]]
    advice: Optional[Mapping[str, Any]]
    bounded_adjustments: List[Dict[str, Any]]
    advice_receipt: Optional[Dict[str, Any]]


def resolve_task_advice(
    *,
    behavioral_advisor: Optional[BehavioralAdvisor],
    routing_context: Dict[str, Any],
    eligible_routes: List[Dict[str, Any]],
    shape: str,
    framework_version: str,
    static_policy_hash: str,
    eligible_choice_hash: str,
    routing_adjustments: List[Dict[str, Any]],
    stages: Mapping[str, Any],
) -> TaskAdviceResolution:
    """Apply only bounded, transaction-bound advisory preferences."""
    advice = None
    bounded: List[Dict[str, Any]] = []
    if behavioral_advisor is not None:
        advice = behavioral_advisor.advice_for_task(
            context=routing_context,
            eligible_routes=eligible_routes,
            static_policy={
                "profile": shape,
                "framework_version": framework_version,
                "static_policy_hash": static_policy_hash,
                "eligible_choice_hash": eligible_choice_hash,
                "context_identity": comparison_context_hash(routing_context),
            })
        validate_advice_transaction(advice, static_policy={
            "static_policy_hash": static_policy_hash,
            "eligible_choice_hash": eligible_choice_hash,
            "context_identity": comparison_context_hash(routing_context),
        })
        raw_adjustments = (list(advice.get("adjustments", []))
                           if isinstance(advice, Mapping) else [])
        bounded = bounded_adjustments(advice, eligible_routes)
        if len(bounded) != len(raw_adjustments):
            raise ValueError("behavioral advisor returned ineligible or malformed adjustments")
        proposed = routing_adjustments + bounded
        if advice.get("status") not in {"ok", "unavailable"} and advice.get("adjustments"):
            raise ValueError("behavioral advisor returned an invalid response")
        for adjustment in proposed:
            validate_adaptive_adjustment(
                adjustment, static_stages=stages,
                always_on_guards=[
                    guard.get("key") or guard.get("id")
                    for guard in load_guard_pack().get("always_on", [])])
        routing_adjustments = proposed
    else:
        for adjustment in routing_adjustments:
            validate_adaptive_adjustment(
                adjustment, static_stages=stages,
                always_on_guards=[
                    guard.get("key") or guard.get("id")
                    for guard in load_guard_pack().get("always_on", [])])

    routing_context = dict(routing_context)
    routing_context["comparison_context_hash"] = comparison_context_hash(routing_context)
    receipt = None
    if behavioral_advisor is not None:
        receipt = {
            "contract_version": "1.0.0",
            "context": {
                **routing_context,
                "context_identity": comparison_context_hash(routing_context),
                "static_policy_hash": static_policy_hash,
                "eligible_choice_hash": eligible_choice_hash,
                "advice_version": str(
                    advice.get("advice_id") or advice.get("semantic_hash") or "unversioned"),
                "expires_at": advice.get("expires_at"),
                "source_profile_hash": advice.get("profile_pack_hash"),
            },
            "eligible_choices": list(eligible_routes),
            "adjustments": [dict(item) for item in bounded],
            "final_decision": {
                "policy_hash": "", "selected_routes": [],
                "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        }
    return TaskAdviceResolution(
        routing_context, routing_adjustments, advice, bounded, receipt)
