"""Legacy whole-state compatibility for CFW runtime sessions.

Projection-v2 callers do not use this module. It exists only for old persisted
DP state that still needs materializing through the current lifecycle overlay.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping

from runtime_contract.component_control import (
    AppliedControlLease, ComponentControl, ComponentOverlay, COMPONENT_IDS,
)

def component_overlay_from_legacy_behavior_state(value: Mapping[str, Any]) -> ComponentOverlay:
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
    target_leases: list[AppliedControlLease] = []
    for intervention in value.get("active_interventions", ()):
        if not isinstance(intervention, Mapping):
            continue
        baseline = intervention.get("baseline_action_index", 0)
        duration = intervention.get("expires_after_actions", 0)
        intervention_id = intervention.get("intervention_id")
        behavior_revision = intervention.get("start_revision")
        if (type(baseline) is not int or baseline < 0
                or type(duration) is not int or not 1 <= duration <= 64
                or not isinstance(intervention_id, str) or not intervention_id
                or type(behavior_revision) is not int or behavior_revision < 0):
            raise ValueError("DP intervention target lease metadata is malformed")
        expiry = intervention.get("expires_at_action")
        if expiry is None:
            expiry = baseline + duration - 1
        if type(expiry) is not int or expiry < 0:
            raise ValueError("legacy DP absolute intervention expiry is malformed")
        for target in intervention.get("targets", ()):
            if not isinstance(target, Mapping):
                continue
            name = target.get("target")
            previous = target.get("previous_value")
            applied = target.get("applied_value")
            if (not isinstance(name, str) or not name
                    or not isinstance(previous, str) or not isinstance(applied, str)):
                raise ValueError("DP intervention target lease is malformed")
            lease_id = "lease-" + hashlib.sha256(
                f"{intervention_id}\0{name}".encode()).hexdigest()[:32]
            target_leases.append(AppliedControlLease(
                lease_id=lease_id,
                intervention_id=intervention_id,
                target=name,
                applied_value=applied,
                previous_value=previous,
                expires_at_action=expiry,
                behavior_revision=behavior_revision,
            ))

    controls = tuple(ComponentControl(
        component_id=component_id,
        mode=normalized[component_id],
        scope=scope,
        expires_at_action=None,
        source_revision=revision,
    ) for component_id in sorted(COMPONENT_IDS))
    return ComponentOverlay(behavior_revision=revision,
                            behavior_mode=str(value.get("cognitive_mode") or ""),
                            controls=controls, target_leases=tuple(target_leases))


class LegacyExternalBehaviorMethods:
    def _legacy_external_behavior_revision(self) -> int:
        return int(self.legacy_external_behavior_state.get("revision", -1))

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
        if expected_current_revision != self._legacy_external_behavior_revision():
            raise RuntimeError("CFW behavior rollback revision changed; re-inspect required")
        transition = self._last_component_overlay_transition
        prior_overlay = (component_overlay_from_legacy_behavior_state(value)
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
        prior_revision = self._legacy_external_behavior_revision()
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

        overlay = component_overlay_from_legacy_behavior_state(value)
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
