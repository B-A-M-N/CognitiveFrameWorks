"""Deprecated packet-planning calls delegated to CSW's workflow authority."""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from runtime_contract.statework_provider import DEFAULT_STATEWORK_ROOT, statework_provider
from resolver.errors import PacketDependencyCycle, PacketDependencyError, PacketValidationError
from resolver.statework_matching import _provider_root_from_manifests


def _consumed_inputs(sw: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Read declared packet inputs for compatibility callers."""
    inputs = sw.get("inputs")
    if isinstance(inputs, dict):
        required = [str(item) for item in inputs.get("required", ())
                    if item != "task_context"]
        optional = [str(item) for item in inputs.get("optional", ())
                    if item != "task_context"]
    else:
        required = [str(item) for item in sw.get("consumes", ())
                    if item != "task_context"]
        optional = []
    return required, optional


def _load_cow_packet_store(packets: Optional[Iterable[Dict[str, Any]]],
                           cow_root: Path = DEFAULT_STATEWORK_ROOT):
    """Construct a packet store through the CSW-owned provider."""
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
                       operation: Optional[str] = None,
                       trigger: Optional[str] = None,
                       task_shape: Optional[str] = None,
                       phase: Optional[str] = None,
                       domain_tags: Optional[Iterable[str]] = None,
                       requested_flows: Optional[Mapping[str, str]] = None,
                       cow_root: Path = DEFAULT_STATEWORK_ROOT) -> List[Dict[str, Any]]:
    """Compatibility planner backed by CSW's canonical ``resolve_workflows``."""
    del packet_registry  # CSW owns the registry and packet producer contracts.
    warnings.warn(
        "plan_statework_dag is deprecated; call the CSW WorkflowPlanner directly",
        DeprecationWarning, stacklevel=2)
    roots = ([statework] if isinstance(statework, Mapping)
             else list(statework or ()))
    root_ids = sorted({str(item.get("id") if isinstance(item, Mapping) else item)
                       for item in roots
                       if (item.get("id") if isinstance(item, Mapping) else item)})
    if not root_ids:
        return []
    provider_root = _provider_root_from_manifests(stateworks, cow_root)
    provider = statework_provider(provider_root)
    records = [dict(item) for item in (packet_store or {}).values()
               if isinstance(item, Mapping) and item.get("packet_type")]
    identity = {"task_id": str(task_id or "task-unspecified"),
                "subject_ref": subject_ref}
    missing = missing_required if missing_required is not None else None
    context = {
        "requested_statework_ids": root_ids,
        "domains": [],
        "packet_records": records,
        "task_id": str(task_id or "task-unspecified"),
        "subject_ref": subject_ref,
        "statework_subject_refs": dict(statework_subject_refs or {}),
        "observation_context": dict(observation_context or {}),
        "operation": operation,
        "trigger": trigger,
        "task_shape": task_shape,
        "phase": phase,
        "domains": list(domain_tags or ()),
        "requested_flows": dict(requested_flows or {}),
        "allow_missing_required": bool(allow_missing_required),
        "missing_required": missing,
    }
    try:
        plan = provider.resolve_workflows(
            runtime_identity=identity, task_context=context, routing_adjustments=())
        ordered_ids = [str(item.get("id") or "")
                       for item in plan.get("execution_order", ())
                       if isinstance(item, Mapping)]
        manifests = {str(item.get("id")): dict(item)
                     for item in provider.load_stateworks()
                     if isinstance(item, Mapping) and item.get("id")}
        return [manifests[item] for item in ordered_ids if item in manifests]
    except Exception as exc:
        message = str(exc)
        if "cycle" in message.lower():
            raise PacketDependencyCycle(message) from exc
        if "packet" in message.lower():
            raise PacketValidationError(message) from exc
        raise PacketDependencyError(message) from exc


def plan_statework_chain(statework: Optional[Dict[str, Any]],
                         stateworks: List[Dict[str, Any]],
                         packet_registry: Dict[str, Any], **kwargs: Any) -> List[Dict[str, Any]]:
    """Single-root compatibility spelling backed by CSW composition."""
    warnings.warn(
        "plan_statework_chain is deprecated; call the CSW WorkflowPlanner directly",
        DeprecationWarning, stacklevel=2)
    return plan_statework_dag(statework, stateworks, packet_registry, **kwargs)


__all__ = ["_consumed_inputs", "_load_cow_packet_store", "plan_statework_chain",
           "plan_statework_dag"]
