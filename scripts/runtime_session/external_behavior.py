"""DP-owned behavior projection methods applied by the CFW session."""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping

from runtime_contract.component_control import (
    ComponentOverlay, ExternalBehaviorProjection,
)
from runtime_session.behavior_authority import ExternalBehaviorAuthority


class ExternalBehaviorMethods:
    """Compatibility mixin for the session's canonical external behavior state.

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
        if self.external_behavior_projection:
            return int(self.external_behavior_projection.get("behavior_revision", -1))
        return int(self.legacy_external_behavior_state.get("revision", -1))

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

    def apply_legacy_external_behavior_state(self, value: Mapping[str, Any]) -> None:
        """Materialize a DP-owned behavior state through CFW's declared API."""
        self._materialize_legacy_external_behavior_state(value, allow_rollback=False)
    def effective_legacy_external_behavior_state(self) -> Dict[str, Any]:
        """Project live target leases over the canonical last DP policy state."""
        state = json.loads(json.dumps(self.legacy_external_behavior_state, sort_keys=True))
        if not state or self.lifecycle is None:
            return state
        for target, raw_value in self.lifecycle.effective_behavior_target_values().items():
            if target == "mode":
                state["cognitive_mode"] = raw_value
            elif target.startswith("framework:"):
                component_id = target.split(":", 1)[1].lower()
                state["framework_modes"][component_id] = raw_value
            else:
                key = target.removeprefix("control:")
                if key == "focus_criteria":
                    try:
                        state[key] = json.loads(raw_value)
                    except (TypeError, ValueError):
                        continue
                elif key == "recovery_level":
                    try:
                        state[key] = int(raw_value)
                    except (TypeError, ValueError):
                        continue
                else:
                    state[key] = raw_value
        return state
    def expire_external_behavior_leases(self, action_index: int) -> bool:
        """Apply local CFW lease decay while preserving unrelated DP targets."""
        if not self.external_behavior_authority or self.lifecycle is None:
            raise RuntimeError("external behavior authority has not been bound")
        expired = self.lifecycle.expire_behavior_leases(action_index)
        if expired:
            object.__setattr__(self, "active_lifecycle", set(self.lifecycle.active))
        return expired
    def synchronize_legacy_external_behavior_state(self, value: Mapping[str, Any]) -> None:
        """Reconstruct CFW component activation from persisted DP state."""
        self._materialize_legacy_external_behavior_state(
            value, allow_rollback=False, synchronize=True)
    def rollback_legacy_external_behavior_state(
            self, value: Mapping[str, Any], *, expected_current_revision: int) -> None:
        """Restore a prepared prior overlay with an exact revision guard."""
        if expected_current_revision != self.external_behavior_revision:
            raise RuntimeError("CFW behavior rollback revision changed; re-inspect required")
        transition = self._last_component_overlay_transition
        prior_overlay = (ComponentOverlay.from_legacy_behavior_state(value)
                         if isinstance(value, Mapping) and value else None)
        if (transition is not None and self.lifecycle is not None
                and transition.previous_overlay == prior_overlay
                and transition.overlay.behavior_revision == expected_current_revision):
            prior_lifecycle = set(self.active_lifecycle)
            active = self.lifecycle.rollback_component_overlay(
                transition, expected_current_revision=expected_current_revision)
            object.__setattr__(self, "legacy_external_behavior_state",
                               json.loads(json.dumps(dict(value), sort_keys=True)))
            object.__setattr__(self, "active_lifecycle", set(active))
            object.__setattr__(self, "_last_component_overlay_transition", None)
            self._emit_lifecycle_activations(prior_lifecycle)
            return
        self._materialize_legacy_external_behavior_state(value, allow_rollback=True)
    def _materialize_legacy_external_behavior_state(
            self, value: Mapping[str, Any], *, allow_rollback: bool,
            synchronize: bool = False) -> None:
        if not self.external_behavior_authority:
            raise RuntimeError("external behavior authority has not been bound")
        if not isinstance(value, Mapping) or len(value) > 24:
            raise ValueError("DP behavior state must be a bounded mapping")
        expected_fields = {
            "revision", "cognitive_mode", "framework_modes", "active_stateworks",
            "focus_criteria", "attention_lease", "attention_last_seen",
            "mode_lease",
            "verification_intensity", "exploration_budget",
            "retry_policy", "recovery_level", "context_strategy",
            "response_strategy", "active_interventions", "intervention_history",
        }
        if set(value) != expected_fields:
            raise ValueError("DP behavior state does not match the versioned CFW port")
        revision = value.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("DP behavior revision must be a non-negative integer")
        prior_revision = self.external_behavior_revision
        if not allow_rollback and revision < prior_revision:
            raise ValueError("CFW rejects a stale DP behavior revision")
        if (not allow_rollback and revision == prior_revision
                and dict(value) != self.legacy_external_behavior_state):
            raise ValueError("same DP behavior revision cannot carry different state")
        mode = value.get("cognitive_mode")
        behavior_modes = {"RECOVER", "REINSPECT", "DIAGNOSE", "VERIFY",
                          "READY_TO_COMPLETE", "DELIVER", "REPLAN", "BLOCKED"}
        if mode not in behavior_modes:
            raise ValueError("DP supplied an unsupported cognitive mode")
        frameworks = value.get("framework_modes")
        allowed_frameworks = {"owl", "anchor", "fuse", "ward", "flow", "sispis", "dox"}
        allowed_modes = {"off", "passive", "normal", "elevated", "reset", "reopen",
                         "mandatory", "recovery", "constrained"}
        if not isinstance(frameworks, Mapping) or len(frameworks) > 16:
            raise ValueError("DP framework modes are malformed")
        if any(not isinstance(key, str) for key in frameworks):
            raise ValueError("DP framework mode IDs must be strings")
        normalized = {str(key).lower(): item for key, item in frameworks.items()}
        if (set(normalized) - allowed_frameworks
                or len(normalized) != len(frameworks)
                or any(item not in allowed_modes for item in normalized.values())):
            raise ValueError("DP supplied an unsupported CFW component mode")
        if normalized.get("ward") != "mandatory":
            raise PermissionError("DP cannot lower the WARD hard floor")
        controls = {
            "verification_intensity": value.get("verification_intensity"),
            "exploration_budget": value.get("exploration_budget"),
            "retry_policy": value.get("retry_policy"),
            "recovery_level": value.get("recovery_level"),
            "context_strategy": value.get("context_strategy"),
            "response_strategy": value.get("response_strategy"),
            "focus_criteria": value.get("focus_criteria"),
        }
        choices = {
            "verification_intensity": {"passive", "normal", "high", "strict"},
            "exploration_budget": {"disabled", "low", "normal", "bounded"},
            "retry_policy": {"normal", "materially_different_only", "no_retry"},
            "context_strategy": {"standard", "compact", "criterion_focused"},
            "response_strategy": {"direct", "detailed", "structured"},
        }
        if any(controls[key] not in allowed
               for key, allowed in choices.items()):
            raise ValueError("DP supplied an unsupported CFW control value")
        recovery = controls["recovery_level"]
        if type(recovery) is not int or not 0 <= recovery <= 3:
            raise ValueError("DP recovery level must be between zero and three")
        focus = controls["focus_criteria"]
        if (not isinstance(focus, (tuple, list)) or len(focus) > 8
                or any(not isinstance(item, str) or not item or len(item) > 128
                       for item in focus)):
            raise ValueError("DP focus criteria are malformed")
        attention_lease = value.get("attention_lease")
        if attention_lease is not None:
            lease_fields = {"lease_id", "criterion_ids", "issued_revision",
                            "goal_set_revision", "start_action", "max_actions", "reason",
                            "material_progress_revisions", "continuous_attention_leases"}
            lease_criteria = (attention_lease.get("criterion_ids")
                              if isinstance(attention_lease, Mapping) else None)
            if (not isinstance(attention_lease, Mapping)
                    or set(attention_lease) != lease_fields
                    or not isinstance(lease_criteria, (tuple, list))
                    or tuple(lease_criteria) != tuple(focus)
                    or len(focus) == 0
                    or len(focus) > 3
                    or len(focus) != len(set(focus))
                    or not isinstance(attention_lease.get("lease_id"), str)
                    or not attention_lease["lease_id"]
                    or len(attention_lease["lease_id"]) > 128
                    or type(attention_lease.get("issued_revision")) is not int
                    or attention_lease["issued_revision"] < 0
                    or attention_lease["issued_revision"] > revision
                    or type(attention_lease.get("goal_set_revision")) is not int
                    or attention_lease["goal_set_revision"] < 0
                    or type(attention_lease.get("start_action")) is not int
                    or attention_lease["start_action"] < 0
                    or type(attention_lease.get("max_actions")) is not int
                    or not 1 <= attention_lease["max_actions"] <= 8
                    or not isinstance(attention_lease.get("reason"), str)
                    or not attention_lease["reason"]
                    or len(attention_lease["reason"]) > 128):
                raise ValueError("DP attention lease is malformed or inconsistent")
        mode_lease = value.get("mode_lease")
        if mode_lease is not None:
            mode_lease_fields = {
                "mode", "entered_action", "minimum_actions", "max_actions",
                "cause", "exit_condition", "baseline_approach_epoch"}
            if not isinstance(mode_lease, Mapping) or set(mode_lease) != mode_lease_fields:
                raise ValueError("DP mode lease does not match the versioned CFW port")
            if (mode_lease.get("mode") not in behavior_modes
                    or type(mode_lease.get("entered_action")) is not int
                    or mode_lease["entered_action"] < 0
                    or type(mode_lease.get("minimum_actions")) is not int
                    or mode_lease["minimum_actions"] < 0
                    or type(mode_lease.get("max_actions")) is not int
                    or mode_lease["max_actions"] < mode_lease["minimum_actions"]
                    or not isinstance(mode_lease.get("cause"), str)
                    or not mode_lease["cause"]
                    or len(mode_lease["cause"]) > 128
                    or not isinstance(mode_lease.get("exit_condition"), str)
                    or not mode_lease["exit_condition"]
                    or len(mode_lease["exit_condition"]) > 128):
                raise ValueError("DP mode lease is malformed")
            # DP emits bounded text or null here, not an integer counter.
            baseline_epoch = mode_lease.get("baseline_approach_epoch")
            if baseline_epoch is not None and (
                    not isinstance(baseline_epoch, str)
                    or not baseline_epoch or len(baseline_epoch) > 256):
                raise ValueError("DP mode lease baseline epoch is malformed")

        if attention_lease is not None:
            lease_criteria = tuple(attention_lease.get("criterion_ids") or ())
            material = attention_lease.get("material_progress_revisions")
            if not isinstance(material, Mapping) or len(material) > 3:
                raise ValueError("DP material-progress tracking is malformed")
            for key, item in material.items():
                if (key not in lease_criteria
                        or type(item) is not int or item < 0):
                    raise ValueError("DP material-progress tracking is malformed")
            # DP models both lease fields as a criterion-keyed count mapping.
            continuous = attention_lease.get("continuous_attention_leases")
            if not isinstance(continuous, Mapping) or len(continuous) > 3:
                raise ValueError("DP continuous attention lease count is malformed")
            for key, item in continuous.items():
                if key not in lease_criteria or type(item) is not int or item < 0:
                    raise ValueError("DP continuous attention lease entry is malformed")
                if item > 2:
                    raise ValueError("DP continuous attention lease count exceeds two")

        attention_last_seen = value.get("attention_last_seen")
        if (not isinstance(attention_last_seen, Mapping)
                or len(attention_last_seen) > 256
                or any(not isinstance(key, str) or not key or len(key) > 128
                       or type(item) is not int or item < 0
                       for key, item in attention_last_seen.items())):
            raise ValueError("DP attention history is malformed")

        interventions = value.get("active_interventions")
        if not isinstance(interventions, (tuple, list)) or len(interventions) > 16:
            raise ValueError("DP active interventions are malformed")
        intervention_action_index = 0
        for intervention in interventions:
            if not isinstance(intervention, Mapping):
                raise ValueError("DP active intervention is malformed")
            baseline = intervention.get("baseline_action_index")
            expiry = intervention.get("expires_after_actions")
            if (type(baseline) is not int or baseline < 0
                    or type(expiry) is not int or not 1 <= expiry <= 64):
                raise ValueError("DP intervention lease bounds are malformed")
            intervention_action_index = max(intervention_action_index, baseline)

        overlay = ComponentOverlay.from_legacy_behavior_state(value)
        if self.lifecycle is None:
            raise RuntimeError("CFW lifecycle controller is unavailable for a DP overlay")
        self.lifecycle.synchronize_action_index(intervention_action_index)
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

        # The validated overlay changes actual lifecycle activation. The
        # behavior context below only describes the applied runtime state.
        object.__setattr__(self, "legacy_external_behavior_state",
                           json.loads(json.dumps(dict(value), sort_keys=True)))
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
