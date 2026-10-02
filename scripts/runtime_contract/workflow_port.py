"""Neutral host contract for optional domain workflow engines.

CFW does not select domain workflows or implement their state machines. A
host may supply this structural port when it composes CFW with a workflow
engine such as CSW. The implementation and authority remain with that engine;
the port contains no CSW imports or CFW-specific provider loading.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Tuple, runtime_checkable


def _freeze_records(values: Any, field_name: str) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"workflow {field_name} must be a list or tuple")
    records = []
    for value in values:
        if not isinstance(value, Mapping):
            raise TypeError(f"workflow {field_name} entries must be mappings")
        records.append(MappingProxyType(dict(value)))
    return tuple(records)


@dataclass(frozen=True)
class WorkflowEligibility:
    """Host-computed workflow choices offered to CFW's policy checks."""

    eligible_routes: Tuple[Mapping[str, Any], ...]
    workflow_versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "eligible_routes",
                           _freeze_records(self.eligible_routes, "eligible_routes"))
        versions = dict(self.workflow_versions)
        if any(not isinstance(key, str) or not key or not isinstance(value, str)
               for key, value in versions.items()):
            raise ValueError("workflow versions must map non-empty IDs to version strings")
        object.__setattr__(self, "workflow_versions", MappingProxyType(versions))


@dataclass(frozen=True)
class WorkflowResolution:
    """Host-resolved workflow plan in the runtime's neutral wire shape.

    The host owns matching, prerequisites, legal composition, and flow
    selection. CFW verifies basic shape/scope and pins the returned records;
    it does not interpret workflow manifests or packet registries.
    """

    execution_order: Tuple[Mapping[str, Any], ...]
    active_workflows: Tuple[Mapping[str, Any], ...]
    capsules: Mapping[str, Tuple[str, str]]
    registry_version: str
    dependency_checks_passed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_order",
                           _freeze_records(self.execution_order, "execution_order"))
        object.__setattr__(self, "active_workflows",
                           _freeze_records(self.active_workflows, "active_workflows"))
        normalized_capsules = {}
        for name, value in dict(self.capsules).items():
            if (not isinstance(name, str) or not name or not isinstance(value, (tuple, list))
                    or len(value) != 2 or not all(isinstance(item, str) for item in value)):
                raise ValueError("workflow capsules must map names to (content, hash) strings")
            normalized_capsules[name] = tuple(value)
        object.__setattr__(self, "capsules", MappingProxyType(normalized_capsules))
        if not isinstance(self.registry_version, str):
            raise TypeError("workflow registry_version must be a string")
        if self.dependency_checks_passed is not True:
            raise ValueError("workflow host must attest that dependency and composition checks passed")


@runtime_checkable
class WorkflowPort(Protocol):
    """Minimal host-facing workflow API.

    ``runtime_identity`` and ``task_context`` are host-authored mappings.
    Snapshot and guidance responses must be immutable/revisioned views. A
    step request is only a proposal; the authority validates legality and
    evidence before returning its result.
    """

    def snapshot(self, *, runtime_identity: Mapping[str, Any],
                 task_context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return the authoritative workflow snapshot for this task scope."""
        ...

    def active_guidance(self, *, runtime_identity: Mapping[str, Any],
                        task_context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return current workflow guidance without transferring authority."""
        ...

    def apply_step(self, *, runtime_identity: Mapping[str, Any],
                   step: Mapping[str, Any]) -> Mapping[str, Any]:
        """Request an authority-checked workflow transition."""
        ...

    def eligible_routes(self, *, runtime_identity: Mapping[str, Any],
                        task_context: Mapping[str, Any]) -> WorkflowEligibility:
        """Resolve host-authorized workflow and flow choices for the task."""
        ...

    def resolve_workflows(self, *, runtime_identity: Mapping[str, Any],
                          task_context: Mapping[str, Any],
                          routing_adjustments: Tuple[Mapping[str, Any], ...]
                          ) -> WorkflowResolution:
        """Apply validated preferences and return the final host-owned plan."""
        ...


@runtime_checkable
class WorkflowPlanner(Protocol):
    """Narrow planning boundary used by task composition.

    A planner owns eligibility and final legal composition. Runtime workflow
    authority operations remain on ``WorkflowPort`` when a host needs them.
    """

    def eligible_routes(self, *, runtime_identity: Mapping[str, Any],
                        task_context: Mapping[str, Any]) -> WorkflowEligibility: ...

    def resolve_workflows(self, *, runtime_identity: Mapping[str, Any],
                          task_context: Mapping[str, Any],
                          routing_adjustments: Tuple[Mapping[str, Any], ...]
                          ) -> WorkflowResolution: ...


class ProviderWorkflowPlanner:
    """Translate the neutral provider mapping into the CFW wire records."""

    def __init__(self, provider: Any):
        self.provider = provider

    def eligible_routes(self, *, runtime_identity: Mapping[str, Any],
                        task_context: Mapping[str, Any]) -> WorkflowEligibility:
        result = self.provider.eligible_routes(
            runtime_identity=runtime_identity, task_context=task_context)
        if isinstance(result, WorkflowEligibility):
            return result
        if not isinstance(result, Mapping):
            raise TypeError("workflow provider eligibility must be a mapping")
        return WorkflowEligibility(**dict(result))

    def resolve_workflows(self, *, runtime_identity: Mapping[str, Any],
                          task_context: Mapping[str, Any],
                          routing_adjustments: Tuple[Mapping[str, Any], ...]
                          ) -> WorkflowResolution:
        result = self.provider.resolve_workflows(
            runtime_identity=runtime_identity, task_context=task_context,
            routing_adjustments=routing_adjustments)
        if isinstance(result, WorkflowResolution):
            return result
        if not isinstance(result, Mapping):
            raise TypeError("workflow provider plan must be a mapping")
        return WorkflowResolution(**dict(result))


def validate_workflow_planner(planner: Any) -> WorkflowPlanner:
    required = ("eligible_routes", "resolve_workflows")
    missing = [name for name in required if not callable(getattr(planner, name, None))]
    if missing:
        raise TypeError(f"workflow planner is missing methods: {', '.join(missing)}")
    return planner


def validate_workflow_port(port: Any) -> WorkflowPort:
    """Fail early when a host integration does not implement the port."""
    required = ("snapshot", "active_guidance", "apply_step", "eligible_routes",
                "resolve_workflows")
    missing = [name for name in required if not callable(getattr(port, name, None))]
    if missing:
        raise TypeError(f"workflow port is missing callable methods: {', '.join(missing)}")
    return port
