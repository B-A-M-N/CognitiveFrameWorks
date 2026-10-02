"""Validated, revisioned overlays for runtime component activation."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, Mapping


COMPONENT_IDS = frozenset({"owl", "anchor", "fuse", "ward", "flow", "sispis", "dox"})
COMPONENT_MODES = frozenset({
    "off", "passive", "normal", "elevated", "reset", "reopen",
    "mandatory", "recovery", "constrained",
})
COMPONENT_ALLOWED_MODES = {
    "owl": frozenset({"off", "passive", "normal", "elevated", "reset"}),
    "anchor": frozenset({"off", "passive", "normal", "elevated", "reset",
                          "reopen", "recovery"}),
    "fuse": frozenset({"off", "passive", "normal", "constrained"}),
    "ward": frozenset({"mandatory"}),
    "flow": frozenset({"off", "passive", "normal", "elevated"}),
    "sispis": frozenset({"off", "passive", "normal"}),
    "dox": frozenset({"off", "passive", "normal", "elevated", "reset", "reopen"}),
}
_DP_SCOPE = re.compile(r"^dp-runtime-[0-9a-f]{64}$")


@dataclass(frozen=True)
class ComponentControl:
    component_id: str
    mode: str
    scope: str | None
    expires_at_action: int | None
    source_revision: int

    def __post_init__(self) -> None:
        if self.component_id not in COMPONENT_IDS:
            raise ValueError("component control ID is unsupported")
        if self.mode not in COMPONENT_MODES:
            raise ValueError("component control mode is unsupported")
        if self.mode not in COMPONENT_ALLOWED_MODES[self.component_id]:
            raise ValueError(
                f"mode {self.mode!r} is not defined for component {self.component_id!r}")
        if self.scope is not None and not _DP_SCOPE.fullmatch(self.scope):
            raise ValueError("component control scope is not a DP runtime identity")
        if self.expires_at_action is not None and (
                type(self.expires_at_action) is not int or self.expires_at_action < 0):
            raise ValueError("component control expiry must be a non-negative action index")
        if type(self.source_revision) is not int or self.source_revision < 0:
            raise ValueError("component control source revision must be non-negative")


@dataclass(frozen=True)
class ComponentOverlay:
    behavior_revision: int
    behavior_mode: str
    controls: tuple[ComponentControl, ...]

    def __post_init__(self) -> None:
        if type(self.behavior_revision) is not int or self.behavior_revision < 0:
            raise ValueError("component overlay revision must be non-negative")
        if self.behavior_mode not in {
                "RECOVER", "REINSPECT", "DIAGNOSE", "VERIFY",
                "READY_TO_COMPLETE", "DELIVER", "REPLAN", "BLOCKED"}:
            raise ValueError("component overlay behavior mode is unsupported")
        if not isinstance(self.controls, tuple) or len(self.controls) > len(COMPONENT_IDS):
            raise ValueError("component overlay exceeds the supported component bound")
        ids = [item.component_id for item in self.controls]
        if len(ids) != len(set(ids)):
            raise ValueError("component overlay contains duplicate component IDs")
        if set(ids) != COMPONENT_IDS:
            raise ValueError("component overlay must declare every supported component")
        if any(item.source_revision != self.behavior_revision for item in self.controls):
            raise ValueError("component control revision differs from its overlay")
        wards = [item for item in self.controls if item.component_id == "ward"]
        if len(wards) != 1 or wards[0].mode != "mandatory":
            raise PermissionError("component overlay cannot lower the WARD hard floor")

    @classmethod
    def from_behavior_state(cls, value: Mapping[str, Any]) -> "ComponentOverlay":
        if not isinstance(value, Mapping):
            raise TypeError("DP behavior state must be a mapping")
        revision = value.get("revision")
        modes = value.get("framework_modes")
        if type(revision) is not int or not isinstance(modes, Mapping):
            raise ValueError("DP behavior state lacks a revisioned component-mode map")
        normalized = {str(key).lower(): mode for key, mode in modes.items()}
        if set(normalized) != COMPONENT_IDS:
            raise ValueError("DP behavior state must include every supported component mode")

        scopes = {
            item.get("scope") for item in value.get("active_interventions", ())
            if isinstance(item, Mapping) and isinstance(item.get("scope"), str)
        }
        if len(scopes) > 1:
            raise ValueError("component overlay interventions escape one DP runtime scope")
        scope = next(iter(scopes), None)
        expiries: dict[str, int] = {}
        for intervention in value.get("active_interventions", ()):
            if not isinstance(intervention, Mapping):
                continue
            expiry = intervention.get("baseline_action_index", 0) + intervention.get(
                "expires_after_actions", 0)
            if type(expiry) is not int or expiry < 0:
                continue
            for target in intervention.get("targets", ()):
                if not isinstance(target, Mapping):
                    continue
                name = target.get("target")
                if isinstance(name, str) and name.startswith("framework:"):
                    component_id = name.split(":", 1)[1].lower()
                    if component_id in COMPONENT_IDS:
                        expiries[component_id] = max(expiries.get(component_id, 0), expiry)

        controls = tuple(ComponentControl(
            component_id=component_id,
            mode=normalized[component_id],
            scope=scope,
            expires_at_action=expiries.get(component_id),
            source_revision=revision,
        ) for component_id in sorted(COMPONENT_IDS))
        return cls(behavior_revision=revision,
                   behavior_mode=str(value.get("cognitive_mode") or ""),
                   controls=controls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "behavior_revision": self.behavior_revision,
            "behavior_mode": self.behavior_mode,
            "controls": [{
                "component_id": item.component_id,
                "mode": item.mode,
                "scope": item.scope,
                "expires_at_action": item.expires_at_action,
                "source_revision": item.source_revision,
            } for item in self.controls],
        }

    def by_component(self) -> dict[str, ComponentControl]:
        return {item.component_id: item for item in self.controls}


@dataclass(frozen=True)
class PreparedComponentOverlay:
    """Single-use lifecycle transition prepared against one CFW revision."""

    overlay: ComponentOverlay
    expected_current_revision: int
    previous_overlay: ComponentOverlay | None
    previous_lifecycle_state: Mapping[str, Any] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.overlay, ComponentOverlay):
            raise TypeError("prepared component transition requires a validated overlay")
        if type(self.expected_current_revision) is not int or self.expected_current_revision < -1:
            raise ValueError("prepared component transition has an invalid prior revision")
        if self.previous_overlay is not None and not isinstance(
                self.previous_overlay, ComponentOverlay):
            raise TypeError("prepared component transition has an invalid prior overlay")
        if not isinstance(self.previous_lifecycle_state, Mapping):
            raise TypeError("prepared component transition requires a lifecycle snapshot")
        snapshot = {}
        for key, value in self.previous_lifecycle_state.items():
            snapshot[key] = set(value) if isinstance(value, set) else dict(value)
        object.__setattr__(self, "previous_lifecycle_state",
                           MappingProxyType(snapshot))


__all__ = ["COMPONENT_IDS", "COMPONENT_MODES", "COMPONENT_ALLOWED_MODES",
           "ComponentControl", "ComponentOverlay",
           "PreparedComponentOverlay"]
