"""Guard scope and enforcement-key boundary for host policy composition."""
from __future__ import annotations

from typing import Any, Dict, List


def nested_scope(guard: Dict[str, Any]) -> Dict[str, List[str]]:
    scope = guard.get("scope") or {}
    if not isinstance(scope, dict):
        scope = {}

    def values(name: str, legacy: Any = None) -> List[str]:
        value = scope.get(name, legacy)
        if value is None:
            return []
        return [str(item) for item in value] if isinstance(value, (list, tuple, set)) else [str(value)]

    return {
        "domains": values("domains", guard.get("domain")),
        "stateworks": values("stateworks"),
        "task_shapes": values("task_shapes", guard.get("task_shapes")),
        "models": values("models", guard.get("model")),
        "harnesses": values("harnesses", guard.get("harness")),
    }


def enforcement_key(guard: Dict[str, Any]) -> str:
    return str(guard.get("enforcement_key") or guard.get("key") or guard["id"])
