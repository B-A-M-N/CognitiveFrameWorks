"""Provider-backed workflow selection for task resolution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from runtime_contract.statework_provider import DEFAULT_STATEWORK_ROOT, statework_provider
from runtime_contract.workflow_port import (
    ProviderWorkflowPlanner, WorkflowEligibility, WorkflowPlanner,
    validate_workflow_planner,
)
from resolver.workflow_composition import resolve_provider_workflows


@dataclass(frozen=True)
class TaskWorkflowResolution:
    """Validated workflow inputs and the provider-pinned workflow plan."""

    runtime_identity: Dict[str, Any]
    task_context: Dict[str, Any]
    eligibility: WorkflowEligibility
    workflow_port: WorkflowPlanner


def resolve_task_workflows(
    *,
    workflow_port: Optional[WorkflowPlanner],
    cow_root=DEFAULT_STATEWORK_ROOT,
    namespace_id: str,
    application_id: str,
    application_instance_id: str,
    agent_id: str,
    agent_instance_id: str,
    parent_session_id: Optional[str],
    task_id: str,
    attempt_id: str,
    interaction_id: Optional[str],
    subject_ref: Optional[str],
    shape: str,
    domains: list[str],
    phase: Optional[str],
    operation: Optional[str],
    trigger: Optional[str],
    observation_context: Optional[Mapping[str, Any]],
    requested_flows: Optional[Dict[str, str]],
    requested_statework_ids: tuple[str, ...],
    statework_subject_refs: Dict[str, str],
    packet_store: Optional[Dict[str, Dict[str, Any]]],
    allow_missing_required: bool,
    missing_required: Optional[list[str]],
) -> TaskWorkflowResolution:
    """Resolve legal workflows through the configured StateWork authority."""
    runtime_identity = {
        "namespace_id": namespace_id,
        "application_id": application_id,
        "application_instance_id": application_instance_id,
        "agent_id": agent_id,
        "agent_instance_id": agent_instance_id,
        "session_id": parent_session_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "interaction_id": interaction_id,
        "subject_ref": subject_ref,
    }
    task_context = {
        "task_shape": shape,
        "domains": tuple(domains),
        "phase": phase,
        "operation": operation,
        "trigger": trigger,
        "observation_context": dict(observation_context or {}),
        "current_state": (observation_context or {}).get("state"),
        "requested_flows": dict(requested_flows or {}),
        "requested_statework_ids": tuple(requested_statework_ids),
        "statework_subject_refs": dict(statework_subject_refs),
        "packet_records": (list(packet_store.values())
                           if isinstance(packet_store, Mapping)
                           else list(packet_store or ())),
        "allow_missing_required": allow_missing_required,
        "missing_required": missing_required,
    }
    if workflow_port is None:
        workflow_port = ProviderWorkflowPlanner(statework_provider(cow_root))
    workflow_port = validate_workflow_planner(workflow_port)
    eligibility = workflow_port.eligible_routes(
        runtime_identity=runtime_identity, task_context=task_context)
    if not isinstance(eligibility, WorkflowEligibility):
        raise TypeError("WorkflowPlanner.eligible_routes must return WorkflowEligibility")
    for item in eligibility.eligible_routes:
        if (item.get("route_type") not in {"statework", "flow", "specialist"}
                or not isinstance(item.get("route"), str)
                or not item.get("route")
                or (item.get("route_type") in {"flow", "specialist"}
                    and not isinstance(item.get("statework_id"), str))):
            raise ValueError("WorkflowPlanner returned an invalid eligible workflow route")

    return TaskWorkflowResolution(runtime_identity, task_context, eligibility, workflow_port)


def resolve_prepared_task_workflows(
    prepared: TaskWorkflowResolution,
    *,
    routing_adjustments: tuple[Dict[str, Any], ...],
    statework_subject_refs: Dict[str, str],
    subject_ref: Optional[str],
    domains: list[str],
    phase: Optional[str],
    requested_flows: Optional[Dict[str, str]],
):
    """Compose a plan from the already validated eligibility snapshot."""
    plan = resolve_provider_workflows(
        prepared.workflow_port, prepared.eligibility,
        prepared.runtime_identity, prepared.task_context,
        routing_adjustments, statework_subject_refs, subject_ref)
    if not plan.available and (domains or phase is not None
                               or subject_ref is not None or requested_flows):
        raise ValueError(
            "StateWork capability is required by this task but no registry is installed")
    return plan
