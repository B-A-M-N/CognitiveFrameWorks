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
class WorkflowRouteRanking:
    """Workflow-owner ranking and contract pins for already resolved routes."""

    legal_routes: Tuple[str, ...]
    scores: Mapping[str, int]
    workflow_revision: int
    route_contract_digests: Mapping[str, str]
    registry_hash: str
    workflow_id: str

    def __post_init__(self) -> None:
        routes = tuple(self.legal_routes)
        if (len(routes) > 16 or len(routes) != len(set(routes))
                or any(not isinstance(item, str) or not item or len(item) > 192
                       for item in routes)):
            raise ValueError("workflow legal route IDs are invalid")
        scores = dict(self.scores)
        digests = dict(self.route_contract_digests)
        if (not isinstance(self.workflow_id, str) or not self.workflow_id
                or len(self.workflow_id) > 128
                or type(self.workflow_revision) is not int or self.workflow_revision < 0
                or not isinstance(self.registry_hash, str) or len(self.registry_hash) != 64
                or any(char not in "0123456789abcdef" for char in self.registry_hash)
                or set(scores) != set(routes) or set(digests) != set(routes)
                or len(digests) > 16):
            raise ValueError("workflow route ranking metadata is invalid")
        if any(not isinstance(key, str) or not key or type(value) is not int or value < 0
               for key, value in scores.items()):
            raise ValueError("workflow route scores are invalid")
        if any(not isinstance(key, str) or not key or not isinstance(value, str)
               or len(value) != 64
               or any(char not in "0123456789abcdef" for char in value)
               for key, value in digests.items()):
            raise ValueError("workflow route contract digests are invalid")
        object.__setattr__(self, "legal_routes", routes)
        object.__setattr__(self, "scores", MappingProxyType(scores))
        object.__setattr__(self, "route_contract_digests", MappingProxyType(digests))


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
    component_requirements: Tuple[str, ...]
    component_enhancements: Tuple[str, ...]
    registry_version: str
    registry_hash: str
    dependency_checks_passed: bool
    route_contract_digests: Mapping[str, str] = field(default_factory=dict)
    completion_claim_classes: Mapping[str, Mapping[str, Mapping[str, Any]]] = field(
        default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_order",
                           _freeze_records(self.execution_order, "execution_order"))
        object.__setattr__(self, "active_workflows",
                           _freeze_records(self.active_workflows, "active_workflows"))
        for name in ("component_requirements", "component_enhancements"):
            values = getattr(self, name)
            if (not isinstance(values, (list, tuple)) or len(values) > 64
                    or any(not isinstance(item, str) or not item or len(item) > 128
                           for item in values)
                    or len(values) != len(set(values))):
                raise ValueError(f"workflow {name} must contain unique bounded component IDs")
            object.__setattr__(self, name, tuple(values))
        normalized_capsules = {}
        for name, value in dict(self.capsules).items():
            if (not isinstance(name, str) or not name or not isinstance(value, (tuple, list))
                    or len(value) != 2 or not all(isinstance(item, str) for item in value)):
                raise ValueError("workflow capsules must map names to (content, hash) strings")
            normalized_capsules[name] = tuple(value)
        object.__setattr__(self, "capsules", MappingProxyType(normalized_capsules))
        route_digests = dict(self.route_contract_digests)
        if (len(route_digests) > 64
                or any(not isinstance(route, str) or not route or len(route) > 192
                       or not isinstance(digest, str) or len(digest) != 64
                       or any(char not in "0123456789abcdef" for char in digest)
                       for route, digest in route_digests.items())):
            raise ValueError("workflow route contract digests are invalid")
        object.__setattr__(self, "route_contract_digests",
                           MappingProxyType(route_digests))
        if not isinstance(self.registry_version, str):
            raise TypeError("workflow registry_version must be a string")
        if (not isinstance(self.registry_hash, str)
                or (self.registry_hash and (len(self.registry_hash) != 64
                    or any(char not in "0123456789abcdef"
                           for char in self.registry_hash)))
                or (self.registry_version and not self.registry_hash)):
            raise ValueError("workflow registry_hash must be empty or a SHA-256 digest")
        if self.dependency_checks_passed is not True:
            raise ValueError("workflow host must attest that dependency and composition checks passed")
        completion_classes: dict[str, Mapping[str, Mapping[str, Any]]] = {}
        raw_completion_classes = dict(self.completion_claim_classes)
        if len(raw_completion_classes) > 64:
            raise ValueError("workflow completion contract exceeds its StateWork bound")
        for statework_id, raw_classes in raw_completion_classes.items():
            if (not isinstance(statework_id, str) or not statework_id
                    or len(statework_id) > 128 or not isinstance(raw_classes, Mapping)
                    or len(raw_classes) > 64):
                raise ValueError("workflow completion contract is malformed")
            normalized_classes: dict[str, Mapping[str, Any]] = {}
            for claim_class, raw_requirement in raw_classes.items():
                if (not isinstance(claim_class, str) or not claim_class
                        or len(claim_class) > 128
                        or not isinstance(raw_requirement, Mapping)
                        or set(raw_requirement) != {"minimum_state", "evidence_kinds"}):
                    raise ValueError("workflow completion claim class is malformed")
                minimum_state = raw_requirement.get("minimum_state")
                evidence_kinds = raw_requirement.get("evidence_kinds")
                if (not isinstance(minimum_state, str) or not minimum_state
                        or len(minimum_state) > 128
                        or not isinstance(evidence_kinds, (list, tuple))
                        or not evidence_kinds or len(evidence_kinds) > 32
                        or any(not isinstance(kind, str) or not kind or len(kind) > 128
                               for kind in evidence_kinds)
                        or len(set(evidence_kinds)) != len(evidence_kinds)):
                    raise ValueError("workflow completion claim requirements are malformed")
                normalized_classes[claim_class] = MappingProxyType({
                    "minimum_state": minimum_state,
                    "evidence_kinds": tuple(evidence_kinds),
                })
            completion_classes[statework_id] = MappingProxyType(normalized_classes)
        object.__setattr__(self, "completion_claim_classes",
                           MappingProxyType(completion_classes))


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

    def rank_legal_routes(self, *, runtime_identity: Mapping[str, Any],
                          task_context: Mapping[str, Any],
                          workflow_id: str,
                          candidate_routes: Tuple[str, ...],
                          expected_workflow_revision: int) -> WorkflowRouteRanking:
        """Rank resolved legal routes using the workflow owner's selector."""
        ...

    def validate_route_ranking(self, *, runtime_identity: Mapping[str, Any],
                               task_context: Mapping[str, Any],
                               ranking: WorkflowRouteRanking) -> bool:
        """Revalidate a prior ranking at the workflow authority before use."""
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

    def rank_legal_routes(self, *, runtime_identity: Mapping[str, Any],
                          task_context: Mapping[str, Any], workflow_id: str,
                          candidate_routes: Tuple[str, ...],
                          expected_workflow_revision: int) -> WorkflowRouteRanking: ...

    def validate_route_ranking(self, *, runtime_identity: Mapping[str, Any],
                               task_context: Mapping[str, Any],
                               ranking: WorkflowRouteRanking) -> bool: ...


class ProviderWorkflowPlanner:
    """Translate the neutral provider mapping into the CFW wire records."""

    def __init__(self, provider: Any, *, state_authority: Any = None):
        self.provider = provider
        self.state_authority = state_authority

    def bind_state_authority(self, authority: Any) -> "ProviderWorkflowPlanner":
        """Return a task-scoped planner bound to the runtime's injected authority."""
        return ProviderWorkflowPlanner(self.provider, state_authority=authority)

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

    def rank_legal_routes(self, *, runtime_identity: Mapping[str, Any],
                          task_context: Mapping[str, Any],
                          workflow_id: str,
                          candidate_routes: Tuple[str, ...],
                          expected_workflow_revision: int) -> WorkflowRouteRanking:
        ranker = getattr(self.provider, "rank_legal_routes", None)
        if not callable(ranker) or self.state_authority is None:
            raise RuntimeError("workflow provider has no task-bound route ranking authority")
        result = ranker(
            runtime_identity=runtime_identity, task_context=task_context,
            statework_id=workflow_id, candidate_routes=candidate_routes,
            expected_workflow_revision=expected_workflow_revision,
            state_authority=self.state_authority)
        if isinstance(result, WorkflowRouteRanking):
            return result
        if not isinstance(result, Mapping):
            raise TypeError("workflow provider route ranking must be a mapping")
        ranking = WorkflowRouteRanking(**dict(result))
        if ranking.workflow_id != workflow_id:
            raise ValueError("workflow provider ranked a different workflow scope")
        return ranking

    def validate_route_ranking(self, *, runtime_identity: Mapping[str, Any],
                               task_context: Mapping[str, Any],
                               ranking: WorkflowRouteRanking) -> bool:
        """Re-rank the original legal set so scope, revision, and digests stay live."""
        if not isinstance(ranking, WorkflowRouteRanking):
            raise TypeError("workflow route revalidation requires a typed ranking")
        current = self.rank_legal_routes(
            runtime_identity=runtime_identity, task_context=task_context,
            workflow_id=ranking.workflow_id,
            candidate_routes=ranking.legal_routes,
            expected_workflow_revision=ranking.workflow_revision)
        return current == ranking


def validate_workflow_planner(planner: Any) -> WorkflowPlanner:
    required = ("eligible_routes", "resolve_workflows", "rank_legal_routes",
                "validate_route_ranking")
    missing = [name for name in required if not callable(getattr(planner, name, None))]
    if missing:
        raise TypeError(f"workflow planner is missing methods: {', '.join(missing)}")
    return planner


def validate_workflow_port(port: Any) -> WorkflowPort:
    """Fail early when a host integration does not implement the port."""
    required = ("snapshot", "active_guidance", "apply_step", "eligible_routes",
                "resolve_workflows", "rank_legal_routes", "validate_route_ranking")
    missing = [name for name in required if not callable(getattr(port, name, None))]
    if missing:
        raise TypeError(f"workflow port is missing callable methods: {', '.join(missing)}")
    return port
