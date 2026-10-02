"""Typed packet prerequisite validation and StateWork DAG planning."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from runtime_contract.stateworks import current_packet as _current_packet
from runtime_contract.statework_provider import DEFAULT_STATEWORK_ROOT, statework_provider
from resolver.errors import PacketDependencyCycle, PacketDependencyError, PacketValidationError

def _consumed_inputs(sw: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Required and optional consumed packets. A required packet that is
    missing routes its registered producer first; optional packets never
    recurse (Infrae may accept a prior truth packet when resuming, but never
    requires it)."""
    inputs = sw.get("inputs")
    if isinstance(inputs, dict):
        required = [p for p in inputs.get("required", []) if p != "task_context"]
        optional = [p for p in inputs.get("optional", []) if p != "task_context"]
    else:
        # legacy flat `consumes` bag — treat everything as required
        required = [p for p in sw.get("consumes", []) if p != "task_context"]
        optional = []
    return required, optional

def _load_cow_packet_store(packets: Optional[Iterable[Dict[str, Any]]],
                           cow_root: Path = DEFAULT_STATEWORK_ROOT):
    """Build the provider-validated packet store through the host boundary."""
    try:
        return statework_provider(cow_root).packet_store(packets)
    except Exception as exc:
        raise PacketDependencyError(str(exc)) from exc

def plan_statework_dag(statework: Any, stateworks: List[Dict[str, Any]],
                         packet_registry: Dict[str, Any],
                         packet_store: Optional[Dict[str, Dict[str, Any]]] = None,
                         allow_missing_required: bool = True,
                         missing_required: Optional[List[str]] = None,
                         task_id: Optional[str] = None,
                         subject_ref: Optional[str] = None,
                         statework_subject_refs: Optional[Mapping[str, str]] = None,
                         observation_context: Optional[Mapping[str, Any]] = None,
                         cow_root: Path = DEFAULT_STATEWORK_ROOT) -> List[Dict[str, Any]]:
    """Plan a composable DAG from every desired StateWork root.

    Required packets, declared bundle dependencies, and all roots are merged
    into one deterministic topological order. Any packet that is not present
    and fresh routes its registered producer first.

    DFS with explicit visiting set: a future A -> B -> C -> A contract fails
    with PacketDependencyCycle instead of recursing forever. Unknown required
    packets raise PacketDependencyError (never silently skipped)."""
    roots = ([statework] if isinstance(statework, Mapping)
             else list(statework or ()))
    if not roots:
        return []
    by_id = {m.get("id"): m for m in stateworks}
    producers = packet_registry.get("packets", {})
    valid_packets = [value for value in (packet_store or {}).values()
                     if isinstance(value, dict) and value.get("packet_type")]
    try:
        store = _load_cow_packet_store(valid_packets, cow_root=cow_root)
    except Exception as exc:
        raise PacketValidationError(str(exc)) from exc
    missing = missing_required if missing_required is not None else []
    chain: List[Dict[str, Any]] = []
    resolved: Set[str] = set()
    visiting: Set[str] = set()

    def current_required_packet(statework_id: Any, packet_type: str) -> Optional[Dict[str, Any]]:
        expected_subject = (statework_subject_refs or {}).get(str(statework_id), subject_ref)
        if not expected_subject:
            return None
        return _current_packet(
            store, packet_type, task_id=task_id or "task-unspecified",
            subject_ref=expected_subject, observation_context=observation_context)

    def resolve(sw: Dict[str, Any]) -> None:
        sid = sw.get("id")
        if sid in resolved:
            return
        if sid in visiting:
            raise PacketDependencyCycle(
                f"packet dependency cycle detected at StateWork {sid!r} "
                f"(chain: {sorted(visiting)} -> {sid})")
        visiting.add(sid)
        composition = sw.get("composition") or {}
        for dependency_id in sorted(set(composition.get("requires") or ())):
            required, _optional = _consumed_inputs(sw)
            dependency_packets = [packet for packet in required
                                  if (producers.get(packet) or {}).get("producer") == dependency_id]
            if dependency_packets and all(
                    current_required_packet(sid, packet) is not None
                    for packet in dependency_packets):
                continue
            dependency = by_id.get(dependency_id)
            if dependency is None:
                raise PacketDependencyError(
                    f"StateWork {sid!r} requires unavailable StateWork {dependency_id!r}")
            resolve(dependency)
        required, _optional = _consumed_inputs(sw)
        for packet in required:
            meta = producers.get(packet)
            if not meta:
                if allow_missing_required:
                    missing.append(packet)
                    continue
                raise PacketDependencyError(
                    f"required packet {packet!r} consumed by {sid!r} is not "
                    f"registered in packet-registry.yaml")
            # Subject identity is part of the packet contract.  A task id is
            # not a substitute for the branch/repository/entity being
            # observed; accepting that fallback would permit cross-subject
            # truth to satisfy a prerequisite.
            current = current_required_packet(sid, packet)
            if current is not None:
                continue  # validated typed packet satisfies prerequisite
            producer_id = meta.get("producer")
            producer = by_id.get(producer_id)
            if producer is None:
                raise PacketDependencyError(
                    f"packet {packet} declares producer {producer_id!r} but no "
                    f"StateWork manifest exists")
            resolve(producer)
        visiting.remove(sid)
        resolved.add(sid)
        chain.append(sw)

    for root in roots:
        resolve(root)
    active_ids = {str(item.get("id")) for item in chain}
    root_groups: Dict[str, list[tuple[str, str]]] = {}
    for item in chain:
        composition = item.get("composition") or {}
        group = str(composition.get("group") or "")
        mode = str(composition.get("mode") or "composable")
        conflicts = active_ids.intersection(
            set(composition.get("conflicts") or ()))
        if conflicts:
            raise PacketDependencyError(
                f"StateWork {item.get('id')!r} conflicts with active bundles {sorted(conflicts)}")
    for root in roots:
        sid = str(root.get("id") or "")
        composition = root.get("composition") or {}
        group = str(composition.get("group") or "")
        if group:
            root_groups.setdefault(group, []).append(
                (sid, str(composition.get("mode") or "composable")))

    def is_dependency(consumer_id: str, required_id: str) -> bool:
        pending = [consumer_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            dependencies = set(
                (by_id.get(current, {}).get("composition") or {}).get("requires") or ())
            if required_id in dependencies:
                return True
            pending.extend(str(value) for value in dependencies)
        return False

    for group, members in root_groups.items():
        if len(members) < 2 or not any(mode == "exclusive" for _sid, mode in members):
            continue
        for index, (left, _left_mode) in enumerate(members):
            for right, _right_mode in members[index + 1:]:
                if not is_dependency(left, right) and not is_dependency(right, left):
                    raise PacketDependencyError(
                        f"exclusive StateWork group {group!r} contains independent roots "
                        f"{sorted((left, right))}")
    return chain

def plan_statework_chain(statework: Optional[Dict[str, Any]], stateworks: List[Dict[str, Any]],
                         packet_registry: Dict[str, Any], **kwargs: Any) -> List[Dict[str, Any]]:
    """Backward-compatible single-root spelling for plan_statework_dag."""
    return plan_statework_dag(statework, stateworks, packet_registry, **kwargs)
