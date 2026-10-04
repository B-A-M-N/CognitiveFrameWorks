"""DP-owned behavior projection methods applied by the CFW session."""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping

from runtime_contract.component_control import ComponentOverlay, ExternalBehaviorProjection
from runtime_session.behavior_authority import ExternalBehaviorAuthority


class ExternalBehaviorMethods:
    """Projection-v2 methods for the session's external behavior boundary.

    RuntimeSession remains the state owner; this module groups the validation,
    projection, lease expiry, and overlay application boundary.
    """
    def available_component_ids(self) -> tuple[str, ...]:
        """Return the CFW frameworks present in this immutable task bundle."""
        supported = {"owl", "anchor", "fuse", "ward", "flow", "sispis", "dox"}
        owners = {stage.split("_", 1)[0].lower()
                  for stage in self.bundle.kernel}
        return tuple(sorted(owners & supported))
    def enable_external_behavior_authority(self) -> tuple[str, ...]:
        """Bind DP as the behavior authority before this session sees agent work."""
        if self.external_behavior_authority:
            return self.available_component_ids()
        if self.behavior_authority is not None:
            raise RuntimeError(
                "DigitalPsychology must bind before CFW standalone behavior services are created")
        if (self.system_one_sequence or self.route_decisions
                or self._pending_action_requests or self.attempt_descriptors):
            raise RuntimeError(
                "DigitalPsychology must bind before CFW behavioral activity begins")
        object.__setattr__(self, "_external_startup_lifecycle",
                           set(self.active_lifecycle))
        if self.lifecycle is not None:
            object.__setattr__(self, "_external_startup_lifecycle_state",
                               self.lifecycle.capture_state())
        object.__setattr__(self, "behavior_authority", ExternalBehaviorAuthority())
        object.__setattr__(self, "_deferred_semantic_signal_records", ())
        return self.available_component_ids()
    @property
    def external_behavior_revision(self) -> int:
        return int(self.external_behavior_projection.get("behavior_revision", -1))

    def apply_external_behavior_projection(self, value: Mapping[str, Any]) -> None:
        self._materialize_external_behavior_projection(value, allow_rollback=False)

    def effective_external_behavior_projection(self) -> Dict[str, Any]:
        projection = json.loads(json.dumps(self.external_behavior_projection, sort_keys=True))
        if not projection or self.lifecycle is None:
            return projection
        for target, raw_value in self.lifecycle.effective_behavior_target_values().items():
            if target == "mode":
                projection["cognitive_mode"] = raw_value
            elif target.startswith("framework:"):
                component_id = target.split(":", 1)[1].lower()
                projection["framework_modes"][component_id] = raw_value
            else:
                key = target.removeprefix("control:")
                if key == "focus_criteria":
                    try:
                        projection[key] = json.loads(raw_value)
                    except (TypeError, ValueError):
                        continue
                elif key == "recovery_level":
                    try:
                        projection[key] = int(raw_value)
                    except (TypeError, ValueError):
                        continue
                else:
                    projection[key] = raw_value
        return projection

    def synchronize_external_behavior_projection(self, value: Mapping[str, Any]) -> None:
        self._materialize_external_behavior_projection(
            value, allow_rollback=False, synchronize=True)

    def rollback_external_behavior_projection(
            self, value: Mapping[str, Any], *, expected_current_revision: int) -> None:
        if expected_current_revision != self.external_behavior_revision:
            raise RuntimeError("CFW projection rollback revision changed; re-inspect required")
        transition = self._last_component_overlay_transition
        prior_overlay = (ComponentOverlay.from_projection(value)
                         if isinstance(value, Mapping) and value else None)
        if (transition is not None and self.lifecycle is not None
                and transition.previous_overlay == prior_overlay
                and transition.overlay.behavior_revision == expected_current_revision):
            prior_lifecycle = set(self.active_lifecycle)
            active = self.lifecycle.rollback_component_overlay(
                transition, expected_current_revision=expected_current_revision)
            object.__setattr__(self, "external_behavior_projection",
                               json.loads(json.dumps(dict(value), sort_keys=True)))
            object.__setattr__(self, "active_lifecycle", set(active))
            object.__setattr__(self, "_last_component_overlay_transition", None)
            self._emit_lifecycle_activations(prior_lifecycle)
            return
        self._materialize_external_behavior_projection(value, allow_rollback=True)

    def _materialize_external_behavior_projection(
            self, value: Mapping[str, Any], *, allow_rollback: bool,
            synchronize: bool = False) -> None:
        if not self.external_behavior_authority:
            raise RuntimeError("external behavior authority has not been bound")
        projection = ExternalBehaviorProjection.from_mapping(value)
        canonical = projection.to_dict()
        prior_revision = self.external_behavior_revision
        if not allow_rollback and projection.behavior_revision < prior_revision:
            raise ValueError("CFW rejects a stale DP behavior projection")
        if (not allow_rollback
                and projection.behavior_revision == prior_revision
                and self.external_behavior_projection
                and canonical != self.external_behavior_projection):
            raise ValueError("same DP behavior revision cannot carry different projections")
        overlay = ComponentOverlay.from_projection(projection)
        if self.lifecycle is None:
            raise RuntimeError("CFW lifecycle controller is unavailable for a DP projection")
        self.lifecycle.synchronize_action_index(projection.host_action_index)
        prior_lifecycle = set(self.active_lifecycle)
        prepared_overlay = self.lifecycle.prepare_component_overlay(
            overlay, allow_rollback=allow_rollback)
        if synchronize and not allow_rollback:
            active = self.lifecycle.synchronize_component_overlay(
                overlay, startup_active=set(self._external_startup_lifecycle),
                prepared=prepared_overlay)
        else:
            active = self.lifecycle.apply_component_overlay(
                prepared_overlay, startup_active=set(self._external_startup_lifecycle),
                allow_rollback=allow_rollback)
        object.__setattr__(self, "external_behavior_projection", canonical)
        object.__setattr__(self, "active_lifecycle", set(active))
        object.__setattr__(self, "_last_component_overlay_transition", prepared_overlay)
        self._emit_lifecycle_activations(prior_lifecycle)

    def restore_external_behavior_startup(self) -> None:
        """Restore the immutable CFW startup preset after a failed DP apply."""
        if not self.external_behavior_authority:
            raise RuntimeError("external behavior authority has not been bound")
        object.__setattr__(self, "external_behavior_projection", {})
        object.__setattr__(self, "legacy_external_behavior_state", {})
        object.__setattr__(self, "_last_component_overlay_transition", None)
        if self.lifecycle is not None:
            if self._external_startup_lifecycle_state is None:
                raise RuntimeError("CFW startup lifecycle snapshot is unavailable")
            active = self.lifecycle.restore_state(self._external_startup_lifecycle_state)
        else:
            active = set(self._external_startup_lifecycle)
        object.__setattr__(self, "active_lifecycle",
                           set(active))
