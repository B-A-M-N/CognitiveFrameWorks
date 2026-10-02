"""Task-scoped guard selection, compilation, and rollout assignment."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from runtime_contract.compatibility import validate_compatibility
from resolver.guards import (
    applicable_guards, canary_assigned, compile_task_guards,
    nested_guard_scope,
)


@dataclass(frozen=True)
class TaskGuardResolution:
    pack: Dict[str, Any]
    statework_ids: List[str]
    suppressed_routes: set[str]
    unique_guards: List[Dict[str, Any]]
    eligible_keys: List[str]
    assignments: Dict[str, str]
    assigned_keys: List[str]


def resolve_task_guards(
    *,
    pack: Dict[str, Any],
    stateworks: List[Dict[str, Any]],
    domains: List[str],
    shape: str,
    model: Optional[str],
    harness: Optional[str],
    trigger: Optional[str],
    task_id: str,
    routing_adjustments: List[Dict[str, Any]],
    statework_available: bool,
    statework_registry_version: Optional[str],
    behavior_contract_path: Path,
    load_document,
    load_json,
) -> TaskGuardResolution:
    """Resolve applicable guards and pin their rollout decision for a task."""
    behavior_contract = load_document(behavior_contract_path, load_json)
    behavior_event_version = str(
        behavior_contract.get("properties", {}).get("schema_version", {}).get("const", ""))
    statework_ids = [sw.get("id") for sw in stateworks]
    if statework_available:
        validate_compatibility(
            statework_registry_version=statework_registry_version,
            behavior_event_version=behavior_event_version,
            guard_pack_version=pack.get("pack_version"))

    suppressed_routes = {
        str(item.get("route")) for item in routing_adjustments
        if item.get("route_type") == "guard" and item.get("disposition") == "suppress"
    }
    deployable_guards = [
        guard for guard in pack.get("guards", [])
        if (guard.get("key") or guard.get("id")) not in suppressed_routes
        and guard.get("id") not in suppressed_routes
        and guard.get("family") not in suppressed_routes
    ]
    scoped_pack = dict(pack)
    scoped_pack["guards"] = deployable_guards
    applicable = applicable_guards(
        scoped_pack, domains, [shape], model=model, harness=harness,
        trigger=trigger, task_id=task_id, stateworks=statework_ids)
    unique_guards = compile_task_guards(
        pack["always_on"] + applicable, domains, [shape],
        model=model, harness=harness, trigger=trigger, task_id=task_id,
        token_budget=max(200, sum(
            max(int(guard.get("estimated_tokens", 10) or 10), 1)
            for guard in pack["always_on"])),
        stateworks=statework_ids,
        mandatory_ids=[guard.get("id") for guard in pack["always_on"]],
        routing_adjustments=routing_adjustments)

    def scope_matches(guard: Dict[str, Any]) -> bool:
        scope = nested_guard_scope(guard)
        return not (
            (scope["domains"] and not (set(domains) & set(scope["domains"])))
            or (scope["stateworks"] and not (set(statework_ids) & set(scope["stateworks"])))
            or (scope["task_shapes"] and shape not in set(scope["task_shapes"]))
            or (scope["models"] and (model is None or model not in set(scope["models"])))
            or (scope["harnesses"] and (harness is None or harness not in set(scope["harnesses"])))
            or (guard.get("trigger") is not None and guard.get("trigger") != trigger)
        )

    eligible_keys: List[str] = []
    assignments: Dict[str, str] = {}
    for guard in pack["always_on"] + pack.get("guards", []):
        key = guard.get("key") or guard["id"]
        if key in suppressed_routes or guard.get("id") in suppressed_routes:
            assignments[key] = "ineligible"
            continue
        if guard.get("status") not in {"active", "canary"} or not scope_matches(guard):
            assignments[key] = "ineligible"
            continue
        eligible_keys.append(key)
        if guard.get("status") == "canary" and not canary_assigned(
                key, guard.get("rollout"), task_id):
            assignments[key] = "control"
        else:
            assignments[key] = "treatment"
    assigned_keys = [guard.get("key") or guard["id"] for guard in unique_guards]
    return TaskGuardResolution(
        pack, statework_ids, suppressed_routes, unique_guards,
        eligible_keys, assignments, assigned_keys)
