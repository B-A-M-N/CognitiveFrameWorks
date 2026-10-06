"""Validate and normalize a provider-owned workflow plan for CFW pinning."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from runtime_contract.workflow_port import (
    WorkflowEligibility, WorkflowPlanner, WorkflowResolution,
)
from resolver.common import sha256_bytes


@dataclass(frozen=True)
class PlannedWorkflows:
    execution_order: Tuple[Dict[str, Any], ...]
    active_workflows: Tuple[Dict[str, Any], ...]
    capsules: Mapping[str, Tuple[str, str]]
    component_requirements: Tuple[str, ...]
    component_enhancements: Tuple[str, ...]
    route_contract_digests: Mapping[str, str]
    completion_claim_classes: Mapping[str, Mapping[str, Mapping[str, Any]]]
    registry: Mapping[str, str]
    available: bool


def resolve_provider_workflows(
        planner: WorkflowPlanner, eligibility: WorkflowEligibility,
        runtime_identity: Mapping[str, Any], task_context: Mapping[str, Any],
        routing_adjustments: Tuple[Mapping[str, Any], ...],
        statework_subject_refs: Mapping[str, str],
        default_subject_ref: str | None) -> PlannedWorkflows:
    """Ask the planner for a final plan, then verify its pins before use."""
    result = planner.resolve_workflows(
        runtime_identity=runtime_identity, task_context=task_context,
        routing_adjustments=routing_adjustments)
    if not isinstance(result, WorkflowResolution):
        raise TypeError("WorkflowPlanner.resolve_workflows must return WorkflowResolution")
    chain = tuple(dict(item) for item in result.execution_order)
    active = tuple(dict(item) for item in result.active_workflows)
    capsules = dict(result.capsules)
    component_requirements = tuple(result.component_requirements)
    component_enhancements = tuple(result.component_enhancements)
    route_contract_digests = dict(result.route_contract_digests)
    if (len(component_requirements) > 64 or len(component_enhancements) > 64
            or len(set(component_requirements)) != len(component_requirements)
            or len(set(component_enhancements)) != len(component_enhancements)):
        raise ValueError("WorkflowPlanner returned invalid normalized component requirements")
    if (result.registry_hash and (len(result.registry_hash) != 64
            or any(char not in "0123456789abcdef" for char in result.registry_hash))):
        raise ValueError("WorkflowPlanner returned an invalid workflow registry hash")
    chain_ids = [str(item.get("id") or "") for item in chain]
    active_ids = [str(item.get("id") or "") for item in active]
    if (any(not item_id for item_id in chain_ids + active_ids)
            or len(set(chain_ids)) != len(chain_ids)
            or len(set(active_ids)) != len(active_ids)
            or not set(active_ids).issubset(chain_ids)):
        raise ValueError("WorkflowPlanner returned an inconsistent ordered/active plan")
    for item in active:
        statework_id = str(item["id"])
        expected_subject = statework_subject_refs.get(statework_id, default_subject_ref)
        if not expected_subject or item.get("subject_ref") != expected_subject:
            raise ValueError(
                f"WorkflowPlanner returned an invalid subject binding for {statework_id!r}")
        for required in ("version", "manifest_hash", "runtime_hash"):
            if not isinstance(item.get(required), str) or not item[required]:
                raise ValueError(
                    f"WorkflowPlanner workflow {statework_id!r} has no pinned {required}")
        eligible_version = eligibility.workflow_versions.get(statework_id)
        if eligible_version is None or eligible_version != item["version"]:
            raise ValueError(
                f"WorkflowPlanner workflow {statework_id!r} has no matching eligible version")
    for stage, (content, content_hash) in capsules.items():
        if sha256_bytes(content.encode("utf-8")) != content_hash:
            raise ValueError(f"WorkflowPlanner capsule {stage!r} has an invalid content hash")
    return PlannedWorkflows(
        execution_order=chain,
        active_workflows=active,
        capsules=capsules,
        component_requirements=component_requirements,
        component_enhancements=component_enhancements,
        route_contract_digests=route_contract_digests,
        completion_claim_classes=result.completion_claim_classes,
        registry={"version": result.registry_version, "hash": result.registry_hash},
        available=bool(result.registry_version))
