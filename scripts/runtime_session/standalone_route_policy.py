"""CFW-owned route preference strategy and child policy epoch state."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence

from runtime_contract.workflow_port import WorkflowRouteRanking
from runtime_session.resolver_bridge import resolve_runtime


@dataclass(frozen=True)
class PolicyEpochRecord:
    """Durable identity for a standalone CFW route-policy transition."""

    epoch_index: int
    parent_policy_hash: str
    child_policy_hash: str
    reason: str
    route_type: str
    selected_route: str
    decision_source: str
    decision_ref: Optional[str]
    semantic_decision_ref: Optional[str]
    state_revision: int

    @property
    def system_one_decision_ref(self) -> str:
        """Compatibility accessor for callers reading older route records."""
        return self.semantic_decision_ref or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch_index": self.epoch_index,
            "parent_policy_hash": self.parent_policy_hash,
            "child_policy_hash": self.child_policy_hash,
            "reason": self.reason,
            "route_type": self.route_type,
            "selected_route": self.selected_route,
            "decision_source": self.decision_source,
            "decision_ref": self.decision_ref,
            "semantic_decision_ref": self.semantic_decision_ref,
            "state_revision": self.state_revision,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PolicyEpochRecord":
        data = dict(value)
        if "system_one_decision_ref" in data:
            legacy_ref = data.pop("system_one_decision_ref")
            data.setdefault("decision_source", "semantic_advisory")
            data.setdefault("decision_ref", legacy_ref)
            data.setdefault("semantic_decision_ref", legacy_ref)
        return cls(**data)


@dataclass(frozen=True)
class RouteReplanRecommendation:
    """Standalone route recommendation with workflow-owner contract pins."""

    route: str
    decision_id: str
    source: str
    source_state_digest: str
    request_digest: str
    sequence: int
    confidence: float
    state_revision: int
    workflow_ranking: WorkflowRouteRanking = field(repr=False)
    source_metacognitive_revision: int
    legal_routes_digest: str
    policy_hash: str
    workflow_registry_hash: str
    route_contract_digests: Mapping[str, str] = field(repr=False)
    legal_children: Mapping[str, resolve_runtime.RuntimeBundle] = field(repr=False)
    semantic_decision_ref: Optional[str] = None

    def __post_init__(self) -> None:
        digests = dict(self.route_contract_digests)
        if (not isinstance(self.workflow_ranking, WorkflowRouteRanking)
                or self.workflow_ranking.workflow_revision != self.state_revision
                or not isinstance(self.workflow_registry_hash, str)
                or len(self.workflow_registry_hash) != 64
                or any(char not in "0123456789abcdef"
                       for char in self.workflow_registry_hash)
                or set(digests) != set(self.legal_children)
                or any(not isinstance(key, str) or not key
                       or not isinstance(value, str) or len(value) != 64
                       or any(char not in "0123456789abcdef" for char in value)
                       for key, value in digests.items())):
            raise ValueError("route recommendation workflow pins are malformed")
        if (dict(self.workflow_ranking.route_contract_digests) != digests
                or set(self.workflow_ranking.legal_routes) != set(self.legal_children)):
            raise ValueError("route recommendation differs from its workflow-owner ranking")
        object.__setattr__(self, "route_contract_digests", MappingProxyType(digests))
        object.__setattr__(self, "legal_children",
                           MappingProxyType(dict(self.legal_children)))


@dataclass(frozen=True)
class ChildPolicyEpoch:
    """Standalone CFW child bundle and its explicit route epoch record."""

    record: PolicyEpochRecord
    bundle: resolve_runtime.RuntimeBundle = field(repr=False)


@dataclass(frozen=True)
class SemanticRouteResolution:
    """Result supplied by CFW's semantic tie resolver, with calibration facts."""

    route: Optional[str]
    decision_ref: Optional[str]
    confidence: Optional[float]
    calibrated: bool
    unavailable_reason: Optional[str] = None


@dataclass(frozen=True)
class RoutePolicyContext:
    """Policy inputs; legal candidates always come from the workflow owner."""

    route_preference: Optional[str] = None
    preference_source: str = "metacognition"
    scores: Mapping[str, float] | None = None
    learned_preferred_routes: frozenset[str] = frozenset()
    confidence_threshold: float = 0.0
    resolve_tie: Optional[Callable[[tuple[str, ...]], SemanticRouteResolution]] = None


@dataclass(frozen=True)
class RouteRecommendation:
    """Standalone strategy output, independent of RuntimeSession internals."""

    route: str
    source: str
    reason: str
    confidence: float
    semantic_decision_ref: Optional[str] = None


class RouteSelectionUnavailable(RuntimeError):
    """No policy recommendation can safely be made for the legal candidates."""

    def __init__(self, reason: str, legal_routes: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.legal_routes = tuple(legal_routes)


class StandaloneRoutePolicy:
    """Select a route for standalone CFW and retain its route epochs."""

    _SOURCES = frozenset({"metacognition", "operator", "learned_profile"})
    _REASONS = {
        "metacognition": "deterministic_route_specificity",
        "operator": "operator_route_preference",
        "learned_profile": "learned_profile_route_preference",
        "semantic_advisory": "semantic_route_recommendation",
    }

    def __init__(self, policy_epoch_record: Optional[PolicyEpochRecord] = None) -> None:
        self._policy_epoch_record = policy_epoch_record

    @property
    def policy_epoch_record(self) -> Optional[PolicyEpochRecord]:
        return self._policy_epoch_record

    def restore_policy_epoch(self, record: PolicyEpochRecord) -> None:
        if not isinstance(record, PolicyEpochRecord):
            raise TypeError("policy epoch restore requires a standalone route record")
        if (self._policy_epoch_record is not None
                and self._policy_epoch_record != record):
            raise PermissionError("policy epoch cannot be rebound during journal replay")
        self._policy_epoch_record = record

    def select(self, legal_routes: Sequence[str],
               context: RoutePolicyContext) -> RouteRecommendation:
        """Choose among owner-authorized routes using standalone CFW policy."""
        routes = tuple(legal_routes)
        if (not routes or len(routes) > 16
                or any(not isinstance(route, str) or not route or len(route) > 192
                       for route in routes)
                or len(set(routes)) != len(routes)):
            raise ValueError("standalone route policy requires unique bounded legal routes")
        if not isinstance(context, RoutePolicyContext):
            raise TypeError("standalone route policy requires a typed policy context")

        selected = context.route_preference
        source = context.preference_source
        if source not in self._SOURCES:
            raise ValueError("unsupported deterministic route preference source")
        if selected is not None:
            if selected not in routes:
                raise ValueError(
                    "preferred route must be one of the workflow owner's legal candidates")
            if (source == "learned_profile"
                    and selected not in context.learned_preferred_routes):
                raise PermissionError("learned route preference is not pinned in this policy")
            return RouteRecommendation(
                selected, source, self._REASONS[source], 1.0)

        if len(routes) == 1:
            return RouteRecommendation(
                routes[0], "metacognition", self._REASONS["metacognition"], 1.0)

        scores = dict(context.scores or {})
        if not scores:
            raise RouteSelectionUnavailable(
                "no legal route matches the workflow selector context")
        if (set(scores) - set(routes)
                or any(isinstance(score, bool) or not isinstance(score, (int, float))
                       or not math.isfinite(float(score)) for score in scores.values())):
            raise ValueError("route scores must be finite scores for legal candidates")
        top_score = max(scores.values())
        top_routes = tuple(sorted(route for route, score in scores.items()
                                  if score == top_score))
        if len(top_routes) == 1:
            return RouteRecommendation(
                top_routes[0], "metacognition",
                self._REASONS["metacognition"], 1.0)

        if context.resolve_tie is None:
            raise RouteSelectionUnavailable(
                "legal routes are tied and no calibrated semantic resolver is available",
                top_routes)
        resolution = context.resolve_tie(top_routes)
        if not isinstance(resolution, SemanticRouteResolution):
            raise TypeError("semantic route resolver returned an invalid result")
        if resolution.unavailable_reason:
            raise RouteSelectionUnavailable(resolution.unavailable_reason, top_routes)
        confidence = resolution.confidence
        if (not resolution.calibrated or resolution.route not in top_routes
                or not resolution.decision_ref or confidence is None
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or float(confidence) < context.confidence_threshold):
            raise RouteSelectionUnavailable(
                "semantic route answer is not calibrated for the tied legal candidates",
                top_routes)
        return RouteRecommendation(
            resolution.route, "semantic_advisory",
            self._REASONS["semantic_advisory"], float(confidence),
            semantic_decision_ref=resolution.decision_ref)


__all__ = [
    "ChildPolicyEpoch", "PolicyEpochRecord", "RoutePolicyContext",
    "RouteRecommendation", "RouteReplanRecommendation",
    "RouteSelectionUnavailable", "SemanticRouteResolution",
    "StandaloneRoutePolicy",
]
