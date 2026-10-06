"""Legacy whole-state compatibility for CFW runtime sessions.

Projection-v2 callers do not use this module. It exists only for old persisted
DP state that still needs materializing through the current lifecycle overlay.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

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
                recovery_epoch_id=(intervention.get("recovery_epoch_id")
                                   or intervention.get("condition_id")
                                   or intervention_id),
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
