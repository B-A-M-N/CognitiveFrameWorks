"""DP-owned behavior projection methods applied by the CFW session."""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping

from runtime_contract.component_control import (
    COMPONENT_ALLOWED_MODES, SUPPORTED_BEHAVIOR_COMPONENTS,
    ComponentOverlay, ExternalBehaviorProjection,
    build_task_behavior_contract,
)
from runtime_session.behavior_authority import (
    ExternalBehaviorAuthorityPending, StandaloneBehaviorAuthority,
)


class ExternalBehaviorMethods:
    """Projection-v2 methods for the session's external behavior boundary.

    RuntimeSession remains the state owner; this module groups the validation,
    projection, lease expiry, and overlay application boundary.
    """
    def available_component_ids(self) -> tuple[str, ...]:
        """Return the CFW frameworks present in this immutable task bundle."""
        owners = {stage.split("_", 1)[0].lower()
                  for stage in self.bundle.kernel}
        return tuple(sorted(owners & SUPPORTED_BEHAVIOR_COMPONENTS))

    def task_behavior_contract(self) -> Dict[str, Any]:
        """Describe CFW-owned baseline floors for components in this task bundle.

        Suppression modes encode lifecycle semantics. Reset, reopen, and recovery
        stay transient operations and are not ordered as weaker baseline modes.
        """
        return build_task_behavior_contract(self.available_component_ids())

    def _validate_task_behavior_floor(
            self, projection: ExternalBehaviorProjection) -> None:
        contract = self.task_behavior_contract()
        suppression_modes = contract["suppression_modes"]
        for component, suppressed in suppression_modes.items():
            if projection.framework_modes[component] in suppressed:
                raise PermissionError(
                    f"DP projection lowers the {component.upper()} task baseline floor")
        for lease in projection.target_leases:
            if not lease.target.startswith("framework:"):
                continue
            component = lease.target.split(":", 1)[1].lower()
            suppressed = suppression_modes.get(component, ())
            if lease.applied_value in suppressed or lease.previous_value in suppressed:
                raise PermissionError(
                    f"DP target lease lowers the {component.upper()} task baseline floor")

    def enable_external_behavior_authority(self) -> tuple[str, ...]:
        """Bind DP as the behavior authority before this session sees agent work."""
        if self.behavior.external_authority_bound:
            return self.available_component_ids()
        authority = self.behavior.authority
        if (isinstance(authority, StandaloneBehaviorAuthority)
                and (authority.activated
                     or authority.route_policy.policy_epoch_record is not None)):
            raise RuntimeError(
                "DigitalPsychology must bind before CFW standalone behavior services are used")
        if (authority is not None
                and not isinstance(authority, (ExternalBehaviorAuthorityPending,
                                               StandaloneBehaviorAuthority))):
            raise RuntimeError(
                "DigitalPsychology must bind before CFW standalone behavior services are created")
        if self.actions.pending_action_requests or self.attempts.descriptors:
            raise RuntimeError(
                "DigitalPsychology must bind before CFW behavioral activity begins")
        self.behavior.external_startup_lifecycle = set(
            self.lifecycle_workflow.active_lifecycle)
        if self.lifecycle_workflow.lifecycle is not None:
            self.behavior.external_startup_lifecycle_state = (
                self.lifecycle_workflow.lifecycle.capture_state())
        self.behavior.bind_external_authority()
        return self.available_component_ids()

    def require_external_behavior_authority(self, boundary: str) -> None:
        """Fail closed when combined mode reaches work before adapter binding."""
        if (self.identity_authority.authorities.runtime_mode == "combined_enforcing"
                and not self.behavior.external_authority_bound):
            raise RuntimeError(
                "combined_enforcing requires a bound external behavior authority "
                f"before {boundary}")

    def require_external_behavior_projection(self, boundary: str) -> None:
        """Fail closed when combined mode has no DP state to project."""
        self.require_external_behavior_authority(boundary)
        if (self.identity_authority.authorities.runtime_mode == "combined_enforcing"
                and not self.behavior.external_projection):
            raise RuntimeError(
                "combined_enforcing requires an external behavior projection "
                f"before {boundary}")
    @property
    def external_behavior_revision(self) -> int:
        return int(self.behavior.external_projection.get("behavior_revision", -1))

    def apply_external_behavior_projection(self, value: Mapping[str, Any]) -> None:
        self._materialize_external_behavior_projection(value, allow_rollback=False)

    def effective_external_behavior_projection(self) -> Dict[str, Any]:
        projection = json.loads(json.dumps(self.behavior.external_projection, sort_keys=True))
        if not projection or self.lifecycle_workflow.lifecycle is None:
            return projection
        for target, raw_value in self.lifecycle_workflow.lifecycle.effective_behavior_target_values().items():
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

    def expire_external_behavior_leases(self, action_index: int) -> bool:
        """Expire CFW-local DP target leases at the host action boundary."""
        if not self.behavior.is_external or self.lifecycle_workflow.lifecycle is None:
            raise RuntimeError("external behavior authority has not been bound")
        expired = self.lifecycle_workflow.lifecycle.expire_behavior_leases(action_index)
        if expired:
            self.lifecycle_workflow.active_lifecycle = set(
                self.lifecycle_workflow.lifecycle.active)
        return expired

    def synchronize_external_behavior_projection(self, value: Mapping[str, Any]) -> None:
        self._materialize_external_behavior_projection(
            value, allow_rollback=False, synchronize=True)

    def rollback_external_behavior_projection(
            self, value: Mapping[str, Any], *, expected_current_revision: int) -> None:
        if expected_current_revision != self.external_behavior_revision:
            raise RuntimeError("CFW projection rollback revision changed; re-inspect required")
        transition = self.behavior.last_component_overlay_transition
        prior_overlay = (ComponentOverlay.from_projection(value)
                         if isinstance(value, Mapping) and value else None)
        if (transition is not None and self.lifecycle_workflow.lifecycle is not None
                and transition.previous_overlay == prior_overlay
                and transition.overlay.behavior_revision == expected_current_revision):
            prior_lifecycle = set(self.lifecycle_workflow.active_lifecycle)
            active = self.lifecycle_workflow.lifecycle.rollback_component_overlay(
                transition, expected_current_revision=expected_current_revision)
            self.behavior.external_projection = json.loads(
                json.dumps(dict(value), sort_keys=True))
            self.lifecycle_workflow.active_lifecycle = set(active)
            self.behavior.last_component_overlay_transition = None
            self._emit_lifecycle_activations(prior_lifecycle)
            return
        self._materialize_external_behavior_projection(value, allow_rollback=True)

    def _materialize_external_behavior_projection(
            self, value: Mapping[str, Any], *, allow_rollback: bool,
            synchronize: bool = False) -> None:
        if not self.behavior.external_authority_bound:
            raise RuntimeError("external behavior authority has not been bound")
        projection = ExternalBehaviorProjection.from_mapping(value)
        self._validate_task_behavior_floor(projection)
        canonical = projection.to_dict()
        prior_projection = self.behavior.external_projection
        prior_revision = self.external_behavior_revision
        if not allow_rollback and projection.behavior_revision < prior_revision:
            raise ValueError("CFW rejects a stale DP behavior projection")
        if (not allow_rollback
                and projection.behavior_revision == prior_revision
                and self.behavior.external_projection
                and canonical != self.behavior.external_projection):
            raise ValueError("same DP behavior revision cannot carry different projections")
        overlay = ComponentOverlay.from_projection(projection)
        if self.lifecycle_workflow.lifecycle is None:
            raise RuntimeError("CFW lifecycle controller is unavailable for a DP projection")
        self.lifecycle_workflow.lifecycle.synchronize_action_index(projection.host_action_index)
        prior_lifecycle = set(self.lifecycle_workflow.active_lifecycle)
        prepared_overlay = self.lifecycle_workflow.lifecycle.prepare_component_overlay(
            overlay, allow_rollback=allow_rollback)
        if synchronize and not allow_rollback:
            active = self.lifecycle_workflow.lifecycle.synchronize_component_overlay(
                overlay, startup_active=set(self.behavior.external_startup_lifecycle),
                prepared=prepared_overlay)
        else:
            active = self.lifecycle_workflow.lifecycle.apply_component_overlay(
                prepared_overlay, startup_active=set(self.behavior.external_startup_lifecycle),
                allow_rollback=allow_rollback)
        self.behavior.external_projection = canonical
        self.lifecycle_workflow.active_lifecycle = set(active)
        self.behavior.last_component_overlay_transition = prepared_overlay
        self._emit_lifecycle_activations(prior_lifecycle)
        prior_epochs = {
            str(item["recovery_epoch_id"])
            for item in (prior_projection.get("target_leases") or ())
            if isinstance(item, Mapping)
            and isinstance(item.get("recovery_epoch_id"), str)
        }
        recovery_epoch_ids = sorted({
            str(item.recovery_epoch_id)
            for item in projection.target_leases
            if item.recovery_epoch_id
        })
        new_epochs = [epoch for epoch in recovery_epoch_ids if epoch not in prior_epochs]
        if new_epochs:
            self._emit_runtime({
                "category": "state_transition",
                "event_type": "external_behavior_recovery_epoch_applied",
                "payload": {"summary": "DP recovery behavior applied to CFW",
                            "recovery_epoch_ids": new_epochs[:16],
                            "behavior_revision": projection.behavior_revision},
            })

    def restore_external_behavior_startup(self) -> None:
        """Restore the immutable CFW startup preset after a failed DP apply."""
        if not self.behavior.external_authority_bound:
            raise RuntimeError("external behavior authority has not been bound")
        self.behavior.external_projection = {}
        self.behavior.last_component_overlay_transition = None
        if self.lifecycle_workflow.lifecycle is not None:
            if self.behavior.external_startup_lifecycle_state is None:
                raise RuntimeError("CFW startup lifecycle snapshot is unavailable")
            active = self.lifecycle_workflow.lifecycle.restore_state(self.behavior.external_startup_lifecycle_state)
        else:
            active = set(self.behavior.external_startup_lifecycle)
        self.lifecycle_workflow.active_lifecycle = set(active)
