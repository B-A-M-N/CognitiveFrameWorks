"""Explicit CFW standalone fallback and external-authority markers."""
from __future__ import annotations

from dataclasses import dataclass, field
from runtime_contract.standalone_attention import StandaloneAttentionState
from runtime_session.standalone_route_policy import StandaloneRoutePolicy
from runtime_session.standalone_behavior_policy import (
    StandaloneBehaviorDecision, StandaloneBehaviorPolicy,
)


@dataclass
class StandaloneBehaviorAuthority:
    """Minimal deterministic fallback for standalone CFW sessions."""

    route_policy: StandaloneRoutePolicy = field(default_factory=StandaloneRoutePolicy)
    activated: bool = False
    attention_state: StandaloneAttentionState = field(
        default_factory=StandaloneAttentionState)
    standalone_behavior_policy: StandaloneBehaviorPolicy = field(
        default_factory=StandaloneBehaviorPolicy)
    standalone_behavior_decision: StandaloneBehaviorDecision = field(
        default_factory=lambda: StandaloneBehaviorDecision(
            "DELIVER", "task initialized"))


@dataclass(frozen=True)
class ExternalBehaviorAuthorityPending:
    """Combined mode awaiting its host-selected external behavior adapter."""

    owner: str = "digitalpsychology"
    protocol_version: str = "1.0.0"


@dataclass(frozen=True)
class ExternalBehaviorAuthority:
    """Marker for DP-owned behavior; CFW stores its applied state separately."""

    owner: str = "digitalpsychology"
    protocol_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.owner != "digitalpsychology" or self.protocol_version != "1.0.0":
            raise ValueError("unsupported external behavioral authority")


__all__ = ["ExternalBehaviorAuthority", "ExternalBehaviorAuthorityPending",
           "StandaloneBehaviorAuthority"]
