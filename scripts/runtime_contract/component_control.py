"""Validated, revisioned overlays for runtime component activation."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
from .pipeline import load_pipeline_document


_SOURCE_PIPELINE_PATH = Path(__file__).resolve().parents[2] / "shared" / "pipeline.yaml"
_PACKAGE_PIPELINE_PATH = Path(__file__).with_name("pipeline.yaml")
_PIPELINE_PATH = (_SOURCE_PIPELINE_PATH if _SOURCE_PIPELINE_PATH.is_file()
                  else _PACKAGE_PIPELINE_PATH)
_PIPELINE_DOCUMENT = load_pipeline_document(_PIPELINE_PATH)
_COMPONENT_CATALOG = _PIPELINE_DOCUMENT["components"]
PIPELINE_MANIFEST_VERSION = str(_PIPELINE_DOCUMENT.get("version") or "unknown")
PIPELINE_MANIFEST_HASH = hashlib.sha256(_PIPELINE_PATH.read_bytes()).hexdigest()
if not isinstance(_COMPONENT_CATALOG, Mapping) or not _COMPONENT_CATALOG:
    raise RuntimeError("CFW component catalog is missing or malformed")
_MODEL_VISIBILITY = frozenset({"none", "consequence_only", "guidance"})
if any(component.get("model_visibility") not in _MODEL_VISIBILITY
       for component in _COMPONENT_CATALOG.values()):
    raise RuntimeError("CFW component catalog has missing or invalid model visibility")
COMPONENT_IDS = frozenset(str(item).lower() for item in _COMPONENT_CATALOG)
COMPONENT_MODEL_VISIBILITY = {
    str(component_id).lower(): str(component["model_visibility"])
    for component_id, component in _COMPONENT_CATALOG.items()
}
COMPONENT_ALLOWED_MODES = {
    str(component_id).lower(): frozenset(component.get("allowed_modes") or ())
    for component_id, component in _COMPONENT_CATALOG.items()
}
COMPONENT_BASELINE_MODES = {
    str(component_id).lower(): str(component["baseline_mode"])
    for component_id, component in _COMPONENT_CATALOG.items()
}
COMPONENT_DYNAMIC_CONTROL = {
    str(component_id).lower(): component.get("dynamic_control") is True
    for component_id, component in _COMPONENT_CATALOG.items()
}
COMPONENT_HARD_FLOOR_MODES = {
    str(component_id).lower(): str(component["hard_floor_mode"])
    for component_id, component in _COMPONENT_CATALOG.items()
    if component.get("hard_floor") is True
}
SUPPORTED_BEHAVIOR_COMPONENTS = frozenset({
    "owl", "anchor", "fuse", "ward", "flow", "sispis", "dox",
})
COMPONENT_MODES = frozenset(mode for modes in COMPONENT_ALLOWED_MODES.values()
                            for mode in modes)
_DP_SCOPE = re.compile(r"^dp-runtime-[0-9a-f]{64}$")
MAX_EXTERNAL_TARGET_LEASES = 16 * 12


def build_task_behavior_contract(selected_components: Any) -> dict[str, Any]:
    """Build the canonical CFW-owned baseline contract for one resolved task."""
    if not isinstance(selected_components, (list, tuple, set, frozenset)):
        raise TypeError("selected behavior components must be a bounded collection")
    selected = tuple(sorted({str(item).lower() for item in selected_components
                             if str(item).lower() in SUPPORTED_BEHAVIOR_COMPONENTS}))
    if len(selected) > len(SUPPORTED_BEHAVIOR_COMPONENTS):
        raise ValueError("selected behavior component set is invalid")
    baselines = {component: COMPONENT_BASELINE_MODES[component]
                 for component in selected}
    suppressed = {}
    transient = {}
    operations = {"reset", "reopen", "recovery"}
    for component in selected:
        baseline = baselines[component]
        modes = COMPONENT_ALLOWED_MODES[component]
        if baseline in {"normal", "elevated"}:
            suppressed[component] = sorted(modes & {"off", "passive"})
        elif baseline == "passive":
            suppressed[component] = sorted(modes & {"off"})
        else:
            suppressed[component] = []
        transient[component] = sorted(modes & operations)
    body = {
        "protocol_version": "1.0.0",
        "pipeline_manifest_version": PIPELINE_MANIFEST_VERSION,
        "pipeline_manifest_hash": PIPELINE_MANIFEST_HASH,
        "selected_components": list(selected),
        "baseline_floors": baselines,
        "suppression_modes": suppressed,
        "transient_operation_modes": transient,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return {**body, "contract_hash": hashlib.sha256(
        encoded.encode("utf-8")).hexdigest()}


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
        if (self.component_id in COMPONENT_HARD_FLOOR_MODES
                and self.mode != COMPONENT_HARD_FLOOR_MODES[self.component_id]):
            raise PermissionError(
                f"component {self.component_id!r} is a non-overridable hard floor")
        if (not COMPONENT_DYNAMIC_CONTROL[self.component_id]
                and self.component_id not in COMPONENT_HARD_FLOOR_MODES):
            raise PermissionError(
                f"component {self.component_id!r} is not dynamically controllable")
        if self.scope is not None and not _DP_SCOPE.fullmatch(self.scope):
            raise ValueError("component control scope is not a DP runtime identity")
        if self.expires_at_action is not None and (
                type(self.expires_at_action) is not int or self.expires_at_action < 0):
            raise ValueError("component control expiry must be a non-negative action index")
        if type(self.source_revision) is not int or self.source_revision < 0:
            raise ValueError("component control source revision must be non-negative")


@dataclass(frozen=True)
class AppliedControlLease:
    """Target-scoped DP control layer owned by the CFW runtime overlay."""

    lease_id: str
    intervention_id: str
    target: str
    applied_value: str
    previous_value: str
    expires_at_action: int
    behavior_revision: int
    recovery_epoch_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("lease_id", "intervention_id", "target",
                     "applied_value", "previous_value"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise ValueError(f"applied behavior lease {name} is invalid")
        if (type(self.expires_at_action) is not int or self.expires_at_action < 0
                or type(self.behavior_revision) is not int or self.behavior_revision < 0):
            raise ValueError("applied behavior lease bounds are invalid")
        if (self.recovery_epoch_id is not None
                and (not isinstance(self.recovery_epoch_id, str)
                     or not self.recovery_epoch_id or len(self.recovery_epoch_id) > 128)):
            raise ValueError("applied behavior lease recovery epoch ID is malformed")
        if self.target == "mode":
            modes = {"RECOVER", "REINSPECT", "DIAGNOSE", "VERIFY",
                     "READY_TO_COMPLETE", "DELIVER", "REPLAN", "BLOCKED"}
            if self.applied_value not in modes or self.previous_value not in modes:
                raise ValueError("applied behavior lease mode is unsupported")
        elif self.target.startswith("framework:"):
            component_id = self.target.split(":", 1)[1].lower()
            if (component_id not in COMPONENT_IDS
                    or self.applied_value not in COMPONENT_ALLOWED_MODES[component_id]
                    or self.previous_value not in COMPONENT_ALLOWED_MODES[component_id]):
                raise ValueError("applied behavior lease component mode is unsupported")
            if (component_id in COMPONENT_HARD_FLOOR_MODES
                    and (self.applied_value != COMPONENT_HARD_FLOOR_MODES[component_id]
                         or self.previous_value != COMPONENT_HARD_FLOOR_MODES[component_id])):
                raise PermissionError("applied behavior lease cannot lower a hard floor")
        else:
            if not self.target.startswith("control:"):
                raise ValueError("applied behavior lease target is not canonical")
            target = self.target.removeprefix("control:")
            choices = {
                "verification_intensity": {"passive", "normal", "high", "strict"},
                "exploration_budget": {"disabled", "low", "normal", "bounded"},
                "retry_policy": {"normal", "materially_different_only", "no_retry"},
                "context_strategy": {"standard", "compact", "criterion_focused"},
                "response_strategy": {"direct", "detailed", "structured"},
            }
            if target == "recovery_level":
                if (not self.applied_value.isdigit() or not self.previous_value.isdigit()
                        or not 0 <= int(self.applied_value) <= 3
                        or not 0 <= int(self.previous_value) <= 3):
                    raise ValueError("applied behavior lease recovery level is unsupported")
            elif target == "focus_criteria":
                try:
                    applied, previous = (json.loads(self.applied_value),
                                         json.loads(self.previous_value))
                except (TypeError, ValueError) as exc:
                    raise ValueError("applied behavior lease focus is malformed") from exc
                for values in (applied, previous):
                    if (not isinstance(values, list) or len(values) > 8
                            or len(values) != len(set(values))
                            or any(not isinstance(item, str) or not item or len(item) > 128
                                   for item in values)):
                        raise ValueError("applied behavior lease focus is malformed")
            elif (target not in choices
                  or self.applied_value not in choices[target]
                  or self.previous_value not in choices[target]):
                raise ValueError("applied behavior lease control is unsupported")


@dataclass(frozen=True)
class ExternalBehaviorProjection:
    """Narrow DP actuation state accepted by the CFW runtime boundary."""

    protocol_version: str
    behavior_revision: int
    host_action_index: int
    cognitive_mode: str
    framework_modes: Mapping[str, str]
    focus_criteria: tuple[str, ...]
    verification_intensity: str
    exploration_budget: str
    retry_policy: str
    recovery_level: int
    context_strategy: str
    response_strategy: str
    target_leases: tuple[AppliedControlLease, ...] = ()

    def __post_init__(self) -> None:
        if self.protocol_version != "2.1.0":
            raise ValueError("unsupported CFW behavior projection version")
        if type(self.behavior_revision) is not int or self.behavior_revision < 0:
            raise ValueError("external behavior revision is invalid")
        if type(self.host_action_index) is not int or self.host_action_index < 0:
            raise ValueError("external behavior action index is invalid")
        if self.cognitive_mode not in {
                "RECOVER", "REINSPECT", "DIAGNOSE", "VERIFY",
                "READY_TO_COMPLETE", "DELIVER", "REPLAN", "BLOCKED"}:
            raise ValueError("external cognitive mode is unsupported")
        if not isinstance(self.framework_modes, Mapping):
            raise ValueError("external framework modes are malformed")
        modes = {str(key).lower(): value for key, value in self.framework_modes.items()}
        if set(modes) != COMPONENT_IDS:
            raise ValueError("external projection must declare every CFW component mode")
        for component_id, mode in modes.items():
            if mode not in COMPONENT_ALLOWED_MODES[component_id]:
                raise ValueError(f"external mode for {component_id!r} is unsupported")
        if any(modes.get(key) != mode
               for key, mode in COMPONENT_HARD_FLOOR_MODES.items()):
            raise PermissionError("external projection lowers a CFW hard floor")
        focus = tuple(self.focus_criteria)
        if (len(focus) > 8 or len(focus) != len(set(focus))
                or any(not isinstance(item, str) or not item or len(item) > 128
                       for item in focus)):
            raise ValueError("external focus criteria are malformed")
        choices = {
            "verification_intensity": {"passive", "normal", "high", "strict"},
            "exploration_budget": {"disabled", "low", "normal", "bounded"},
            "retry_policy": {"normal", "materially_different_only", "no_retry"},
            "context_strategy": {"standard", "compact", "criterion_focused"},
            "response_strategy": {"direct", "detailed", "structured"},
        }
        for name, allowed in choices.items():
            value = getattr(self, name)
            if value not in allowed:
                raise ValueError(f"external {name} is unsupported")
        if type(self.recovery_level) is not int or not 0 <= self.recovery_level <= 3:
            raise ValueError("external recovery level is unsupported")
        leases = tuple(self.target_leases)
        if (len(leases) > MAX_EXTERNAL_TARGET_LEASES
                or any(not isinstance(item, AppliedControlLease) for item in leases)
                or len({item.lease_id for item in leases}) != len(leases)):
            raise ValueError("external target leases are malformed or exceed their bound")
        if any(item.behavior_revision > self.behavior_revision for item in leases):
            raise ValueError("external target lease comes from a future behavior revision")
        object.__setattr__(self, "framework_modes", MappingProxyType(modes))
        object.__setattr__(self, "focus_criteria", focus)
        object.__setattr__(self, "target_leases", leases)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExternalBehaviorProjection":
        expected = {
            "protocol_version", "behavior_revision", "host_action_index",
            "cognitive_mode", "framework_modes", "focus_criteria",
            "verification_intensity", "exploration_budget", "retry_policy",
            "recovery_level", "context_strategy", "response_strategy",
            "target_leases",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("CFW behavior projection does not match the v2 contract")
        raw_leases = value["target_leases"]
        if not isinstance(raw_leases, (tuple, list)) or len(raw_leases) > MAX_EXTERNAL_TARGET_LEASES:
            raise ValueError("CFW target lease collection exceeds its bound")
        lease_fields = {"lease_id", "intervention_id", "target", "previous_value",
                        "applied_value", "expires_at_action", "source_revision",
                        "recovery_epoch_id"}
        if any(not isinstance(item, Mapping) or set(item) != lease_fields
               for item in raw_leases):
            raise ValueError("CFW target lease does not match the v2 contract")
        leases = tuple(AppliedControlLease(
            lease_id=str(item["lease_id"]),
            intervention_id=str(item["intervention_id"]),
            target=str(item["target"]),
            applied_value=str(item["applied_value"]),
            previous_value=str(item["previous_value"]),
            expires_at_action=item["expires_at_action"],
            behavior_revision=item["source_revision"],
            recovery_epoch_id=str(item["recovery_epoch_id"]),
        ) for item in raw_leases)
        return cls(
            protocol_version=value["protocol_version"],
            behavior_revision=value["behavior_revision"],
            host_action_index=value["host_action_index"],
            cognitive_mode=value["cognitive_mode"],
            framework_modes=value["framework_modes"],
            focus_criteria=tuple(value["focus_criteria"]),
            verification_intensity=value["verification_intensity"],
            exploration_budget=value["exploration_budget"],
            retry_policy=value["retry_policy"],
            recovery_level=value["recovery_level"],
            context_strategy=value["context_strategy"],
            response_strategy=value["response_strategy"],
            target_leases=leases,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "behavior_revision": self.behavior_revision,
            "host_action_index": self.host_action_index,
            "cognitive_mode": self.cognitive_mode,
            "framework_modes": dict(self.framework_modes),
            "focus_criteria": list(self.focus_criteria),
            "verification_intensity": self.verification_intensity,
            "exploration_budget": self.exploration_budget,
            "retry_policy": self.retry_policy,
            "recovery_level": self.recovery_level,
            "context_strategy": self.context_strategy,
            "response_strategy": self.response_strategy,
            "target_leases": [{
                "lease_id": item.lease_id,
                "intervention_id": item.intervention_id,
                "target": item.target,
                "previous_value": item.previous_value,
                "applied_value": item.applied_value,
                "expires_at_action": item.expires_at_action,
                "source_revision": item.behavior_revision,
                "recovery_epoch_id": item.recovery_epoch_id,
            } for item in self.target_leases],
        }


@dataclass(frozen=True)
class ComponentOverlay:
    behavior_revision: int
    behavior_mode: str
    controls: tuple[ComponentControl, ...]
    target_leases: tuple[AppliedControlLease, ...] = ()

    def __post_init__(self) -> None:
        if type(self.behavior_revision) is not int or self.behavior_revision < 0:
            raise ValueError("component overlay revision must be non-negative")
        if self.behavior_mode not in {
                "RECOVER", "REINSPECT", "DIAGNOSE", "VERIFY",
                "READY_TO_COMPLETE", "DELIVER", "REPLAN", "BLOCKED"}:
            raise ValueError("component overlay behavior mode is unsupported")
        if not isinstance(self.controls, tuple) or len(self.controls) > len(COMPONENT_IDS):
            raise ValueError("component overlay exceeds the supported component bound")
        if (not isinstance(self.target_leases, tuple)
                or len(self.target_leases) > MAX_EXTERNAL_TARGET_LEASES
                or any(not isinstance(item, AppliedControlLease)
                       for item in self.target_leases)):
            raise ValueError("component overlay target leases exceed their bound")
        ids = [item.component_id for item in self.controls]
        if len(ids) != len(set(ids)):
            raise ValueError("component overlay contains duplicate component IDs")
        if set(ids) != COMPONENT_IDS:
            raise ValueError("component overlay must declare every supported component")
        if any(item.source_revision != self.behavior_revision for item in self.controls):
            raise ValueError("component control revision differs from its overlay")
        for component_id, floor_mode in COMPONENT_HARD_FLOOR_MODES.items():
            floors = [item for item in self.controls if item.component_id == component_id]
            if len(floors) != 1 or floors[0].mode != floor_mode:
                raise PermissionError(
                    f"component overlay cannot lower the {component_id.upper()} hard floor")

    @classmethod
    def from_projection(cls, value: Mapping[str, Any] | ExternalBehaviorProjection
                        ) -> "ComponentOverlay":
        projection = (value if isinstance(value, ExternalBehaviorProjection)
                      else ExternalBehaviorProjection.from_mapping(value))
        controls = tuple(ComponentControl(
            component_id=component_id,
            mode=projection.framework_modes[component_id],
            scope=None,
            expires_at_action=None,
            source_revision=projection.behavior_revision,
        ) for component_id in sorted(COMPONENT_IDS))
        return cls(
            behavior_revision=projection.behavior_revision,
            behavior_mode=projection.cognitive_mode,
            controls=controls,
            target_leases=projection.target_leases,
        )


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
            "target_leases": [{
                "lease_id": item.lease_id,
                "intervention_id": item.intervention_id,
                "target": item.target,
                "applied_value": item.applied_value,
                "previous_value": item.previous_value,
                "expires_at_action": item.expires_at_action,
                "behavior_revision": item.behavior_revision,
                "recovery_epoch_id": item.recovery_epoch_id,
            } for item in self.target_leases],
        }

    def by_component(self) -> dict[str, ComponentControl]:
        return {item.component_id: item for item in self.controls}

    def effective_target_value(self, target: str, action_index: int) -> str | None:
        leases = [item for item in self.target_leases if item.target == target]
        if not leases:
            return None
        active = [item for item in leases if action_index <= item.expires_at_action]
        if active:
            return max(active, key=lambda item: (
                item.behavior_revision, item.lease_id)).applied_value
        return min(leases, key=lambda item: (
            item.behavior_revision, item.lease_id)).previous_value

    def has_expired_target_leases(self, action_index: int) -> bool:
        return any(action_index > item.expires_at_action
                   for item in self.target_leases)


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
           "COMPONENT_BASELINE_MODES",
           "COMPONENT_DYNAMIC_CONTROL", "COMPONENT_HARD_FLOOR_MODES",
           "COMPONENT_MODEL_VISIBILITY", "MAX_EXTERNAL_TARGET_LEASES",
           "AppliedControlLease", "ComponentControl", "ComponentOverlay",
           "ExternalBehaviorProjection", "PreparedComponentOverlay"]
