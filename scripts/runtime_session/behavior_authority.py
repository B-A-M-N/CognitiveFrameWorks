"""Explicit CFW behavioral authority strategies."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from runtime_contract.metacognition import (
    MetaDecision, MetacognitiveController, SemanticSignalStore, SemanticSignals,
)
from runtime_contract.system_one import BehavioralJudge
from runtime_contract.system_one_state import SystemOneStateProjector


@dataclass
class StandaloneBehaviorAuthority:
    """CFW-owned metacognition and optional semantic provider services."""

    system_one_judge: Optional[BehavioralJudge] = None
    system_one_projector: SystemOneStateProjector = field(
        default_factory=SystemOneStateProjector)
    metacognitive_controller: MetacognitiveController = field(
        default_factory=MetacognitiveController)
    semantic_signals: SemanticSignals = field(default_factory=SemanticSignals)
    semantic_signal_store: SemanticSignalStore = field(
        default_factory=SemanticSignalStore)
    metacognitive_decision: MetaDecision = field(
        default_factory=lambda: MetaDecision("DELIVER", "task initialized"))


@dataclass(frozen=True)
class ExternalBehaviorAuthority:
    """Marker for DP-owned behavior; CFW stores its applied state separately."""

    owner: str = "digitalpsychology"
    protocol_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.owner != "digitalpsychology" or self.protocol_version != "1.0.0":
            raise ValueError("unsupported external behavioral authority")


__all__ = ["ExternalBehaviorAuthority", "StandaloneBehaviorAuthority"]
