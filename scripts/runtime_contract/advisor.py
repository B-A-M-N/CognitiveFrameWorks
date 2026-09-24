"""Optional behavioral-advice boundary.

The runtime can run entirely from static CFW policy and local artifacts.  An
external DigitalPsychology/MCP adapter may provide bounded preferences, but
the host still owns route eligibility and validates every adjustment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol


class BehavioralAdvisor(Protocol):
    def advice_for_task(self, *, context: Mapping[str, Any],
                        eligible_routes: list[Mapping[str, Any]],
                        static_policy: Mapping[str, Any]) -> Mapping[str, Any]: ...


class NullBehavioralAdvisor:
    """Static-policy fallback used when no external advisor is configured."""

    def advice_for_task(self, *, context: Mapping[str, Any],
                        eligible_routes: list[Mapping[str, Any]],
                        static_policy: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"status": "unavailable", "adjustments": []}


class MCPBehavioralAdvisor:
    """Dependency-free adapter around a host-provided MCP tool caller.

    The callable is deliberately injected so CFW does not import an MCP or DP
    package.  Transport failures are fail-open to static policy, while
    malformed advice is discarded by ``bounded_adjustments``.
    """

    def __init__(self, call_tool: Callable[..., Mapping[str, Any]]) -> None:
        self._call_tool = call_tool

    def advice_for_task(self, *, context: Mapping[str, Any],
                        eligible_routes: list[Mapping[str, Any]],
                        static_policy: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            response = self._call_tool(
                context=dict(context), eligible_routes=list(eligible_routes),
                static_policy=dict(static_policy))
        except Exception as exc:  # external service is advisory only
            return {"status": "unavailable", "reason": str(exc), "adjustments": []}
        if not isinstance(response, Mapping):
            return {"status": "unavailable", "reason": "malformed advice", "adjustments": []}
        return response


def validate_advice_transaction(advice: Mapping[str, Any], *,
                                 static_policy: Mapping[str, Any]) -> None:
    """Reject stale or cross-request advice as one atomic transaction."""
    if not isinstance(advice, Mapping):
        raise ValueError("behavioral advice response is not an object")
    if advice.get("status") == "unavailable" and not advice.get("adjustments"):
        return
    mismatches = []
    for field in ("static_policy_hash", "eligible_choice_hash", "context_identity"):
        if advice.get(field) != static_policy.get(field):
            mismatches.append(field)
    expires_at = advice.get("expires_at")
    if expires_at is not None:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("behavioral advice has invalid expires_at") from exc
        if expiry.tzinfo is None:
            raise ValueError("behavioral advice expiry must include a timezone")
        if expiry <= datetime.now(timezone.utc):
            mismatches.append("expires_at")
    if mismatches:
        raise ValueError("behavioral advice transaction rejected: " + ", ".join(mismatches))


def bounded_adjustments(advice: Mapping[str, Any],
                        eligible_routes: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep only host-declared, non-activating adaptive preferences."""
    allowed = {(str(item.get("route_type") or "*"), str(item.get("route") or ""))
               for item in eligible_routes if item.get("route")}
    result: list[dict[str, Any]] = []
    for item in advice.get("adjustments", []) if isinstance(advice, Mapping) else []:
        if not isinstance(item, Mapping):
            continue
        route = str(item.get("route") or "")
        route_type = str(item.get("route_type") or "*")
        if item.get("disposition") not in {"prefer", "suppress"} or not route:
            continue
        if (route_type, route) not in allowed and ("*", route) not in allowed:
            continue
        result.append(dict(item))
    return result
