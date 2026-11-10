"""Capability snapshot + control locality contract (CFW slice A).

Covers: the host-facing capability snapshot is bounded and revisioned; a
DP/Jev preference can never directly modify the component catalog or bypass
component-mode / hard-floor / non-dynamic-control validation (fail-closed);
the catalog remains canonical.
"""
from __future__ import annotations

import json
from types import MappingProxyType, SimpleNamespace

import pytest

from runtime_contract.component_control import (
    CAPABILITY_SNAPSHOT_VERSION,
    COMPONENT_ALLOWED_MODES,
    COMPONENT_BASELINE_MODES,
    COMPONENT_DYNAMIC_CONTROL,
    COMPONENT_HARD_FLOOR_MODES,
    COMPONENT_IDS,
    COMPONENT_MODEL_VISIBILITY,
    COMPONENT_SCOPE,
    MAX_CAPABILITY_SNAPSHOT_BYTES,
    MAX_EXTERNAL_TARGET_LEASES,
    MAX_FOCUS_CRITERIA,
    PIPELINE_MANIFEST_HASH,
    PIPELINE_MANIFEST_VERSION,
    ComponentControl,
    build_task_behavior_contract,
    get_capability_snapshot,
    validate_component_preference,
)

import digital_psychology_adapter as adapter_module
from digital_psychology_adapter import CFWBehaviorAdapter


def _catalog_fingerprint() -> dict:
    return {
        component_id: {
            "allowed_modes": sorted(COMPONENT_ALLOWED_MODES[component_id]),
            "baseline_mode": COMPONENT_BASELINE_MODES[component_id],
            "dynamic_control": COMPONENT_DYNAMIC_CONTROL[component_id],
            "hard_floor_mode": COMPONENT_HARD_FLOOR_MODES.get(component_id),
            "model_visibility": COMPONENT_MODEL_VISIBILITY[component_id],
            "scope": list(COMPONENT_SCOPE[component_id]),
        }
        for component_id in sorted(COMPONENT_IDS)
    }


def _fake_session(*, revision: int = 7) -> SimpleNamespace:
    contract = build_task_behavior_contract(["owl", "anchor", "ward"])
    scope = "dp-runtime-" + "ab" * 32
    return SimpleNamespace(
        external_behavior_revision=revision,
        bundle=SimpleNamespace(task_id="task-capability"),
        identity_authority=SimpleNamespace(
            identity=SimpleNamespace(
                execution_identity=None,
                session_id="session-1",
                agent_instance_id="agent-1",
                attempt_id="attempt-1",
            )
        ),
        available_component_ids=lambda: tuple(sorted(COMPONENT_IDS)),
        task_behavior_contract=lambda: contract,
        behavior=SimpleNamespace(external_projection=None),
        _dp_scope=scope,
    )


def _fake_adapter(*, revision: int = 7) -> CFWBehaviorAdapter:
    adapter = object.__new__(CFWBehaviorAdapter)
    adapter._session = _fake_session(revision=revision)
    return adapter


# --- snapshot: bounded + revisioned ---------------------------------------

def test_capability_snapshot_bounded_and_revisioned():
    first = get_capability_snapshot(behavior_revision=7)
    assert first["snapshot_version"] == CAPABILITY_SNAPSHOT_VERSION
    assert first["behavior_revision"] == 7
    assert first["pipeline_manifest_version"] == PIPELINE_MANIFEST_VERSION
    assert first["pipeline_manifest_hash"] == PIPELINE_MANIFEST_HASH
    assert set(first["components"]) == set(COMPONENT_IDS)
    encoded = json.dumps(first, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    assert len(encoded.encode("utf-8")) <= MAX_CAPABILITY_SNAPSHOT_BYTES
    # Deterministic: the host can cache and compare by hash.
    again = get_capability_snapshot(behavior_revision=7)
    assert again == first
    assert again["capability_hash"] == first["capability_hash"]
    # Revisioned: a new revision yields a different comparable hash.
    newer = get_capability_snapshot(behavior_revision=8)
    assert newer["behavior_revision"] == 8
    assert newer["capability_hash"] != first["capability_hash"]
    assert newer["components"]["owl"]["lifetime"]["source_revision"] == 8


def test_capability_snapshot_rejects_bad_revision():
    for bad in (-1, "7", 7.0, True, None):
        with pytest.raises(ValueError):
            get_capability_snapshot(behavior_revision=bad)


def test_snapshot_declares_modifiability_scope_lifetime_and_values():
    snapshot = get_capability_snapshot(behavior_revision=3)
    ward = snapshot["components"]["ward"]
    assert ward["modifiable"] is False
    assert ward["hard_floor"] is True
    assert ward["hard_floor_mode"] == "mandatory"
    assert ward["allowed_modes"] == ["mandatory"]
    assert ward["scope"] == list(COMPONENT_SCOPE["ward"])
    assert ward["lifetime"]["kind"] == "overlay-revision"
    owl = snapshot["components"]["owl"]
    assert owl["modifiable"] is True
    assert owl["hard_floor"] is False
    assert owl["hard_floor_mode"] is None
    assert owl["allowed_modes"] == sorted(COMPONENT_ALLOWED_MODES["owl"])
    assert owl["baseline_mode"] == COMPONENT_BASELINE_MODES["owl"]
    assert owl["model_visibility"] == COMPONENT_MODEL_VISIBILITY["owl"]
    for component_id, entry in snapshot["components"].items():
        assert entry["allowed_modes"] == sorted(
            COMPONENT_ALLOWED_MODES[component_id])
        assert entry["scope"] == list(COMPONENT_SCOPE[component_id])
        assert entry["lifetime"]["source_revision"] == 3
    controls = snapshot["controls"]
    assert controls["verification_intensity"]["allowed_values"] == [
        "high", "normal", "passive", "strict"]
    assert controls["recovery_level"]["allowed_values"] == ["0", "1", "2", "3"]
    assert controls["focus_criteria"]["modifiable"] is True
    for entry in controls.values():
        assert entry["modifiable"] is True
        assert entry["scope"] == "target-lease"
        assert entry["lifetime"]["kind"] == "action-index-lease"
        assert (entry["lifetime"]["max_target_leases"]
                == MAX_EXTERNAL_TARGET_LEASES)
    assert snapshot["bounds"]["max_components"] == len(COMPONENT_IDS)
    assert snapshot["bounds"]["max_target_leases"] == MAX_EXTERNAL_TARGET_LEASES
    assert snapshot["bounds"]["max_focus_criteria"] == MAX_FOCUS_CRITERIA
    assert snapshot["authority"]["kind"] == "cfw-component-catalog"
    assert snapshot["authority"]["authority_id"].startswith(
        "cfw-component-catalog:")


# --- illegal direct modification rejected; catalog remains canonical -------

_CATALOG_MAPS = (COMPONENT_ALLOWED_MODES, COMPONENT_BASELINE_MODES,
                 COMPONENT_DYNAMIC_CONTROL, COMPONENT_HARD_FLOOR_MODES,
                 COMPONENT_MODEL_VISIBILITY, COMPONENT_SCOPE)


def test_direct_catalog_modification_rejected():
    for catalog in _CATALOG_MAPS:
        assert isinstance(catalog, MappingProxyType)
        with pytest.raises(TypeError):
            catalog["ward"] = "off"
        with pytest.raises(TypeError):
            del catalog["ward"]
    with pytest.raises(AttributeError):
        COMPONENT_ALLOWED_MODES["owl"].add("turbo")
    for component_id in COMPONENT_IDS:
        assert isinstance(COMPONENT_ALLOWED_MODES[component_id], frozenset)


def test_failed_preference_leaves_catalog_canonical():
    before = _catalog_fingerprint()
    forged = [
        {"ward": "off"},
        {"ward": "passive"},
        {"ward": "normal"},
        {"evil": "off"},
        {"owl": "mandatory"},
        {"owl": "normal", "Ward": "off"},
        {"owl": "normal", "sispis": "turbo"},
    ]
    for preference in forged:
        with pytest.raises((ValueError, PermissionError, TypeError)):
            validate_component_preference(preference)
    with pytest.raises(TypeError):
        validate_component_preference([("owl", "normal")])
    oversized = {f"component-{index}": "off" for index in range(64)}
    with pytest.raises(ValueError):
        validate_component_preference(oversized)
    assert _catalog_fingerprint() == before
    assert PIPELINE_MANIFEST_HASH == get_capability_snapshot(
        behavior_revision=0)["pipeline_manifest_hash"]


def test_snapshot_returns_copies_not_catalog_aliases():
    snapshot = get_capability_snapshot(behavior_revision=5)
    assert type(snapshot["components"]) is dict
    assert type(snapshot["components"]["owl"]) is dict
    assert type(snapshot["components"]["owl"]["allowed_modes"]) is list
    snapshot["components"]["owl"]["allowed_modes"].append("turbo")
    snapshot["components"]["ward"]["modifiable"] = True
    snapshot["controls"]["verification_intensity"]["allowed_values"].append(
        "turbo")
    fresh = get_capability_snapshot(behavior_revision=5)
    assert "turbo" not in fresh["components"]["owl"]["allowed_modes"]
    assert fresh["components"]["ward"]["modifiable"] is False
    assert "turbo" not in fresh["controls"]["verification_intensity"][
        "allowed_values"]
    assert "turbo" not in COMPONENT_ALLOWED_MODES["owl"]


# --- fail-closed preference gate ------------------------------------------

def test_forged_preference_cannot_change_pinned_control_or_ward_floor():
    # Every WARD deviation is forbidden: WARD is a non-overridable hard floor.
    for mode in ("off", "passive", "normal", "elevated", "turbo"):
        with pytest.raises(PermissionError):
            validate_component_preference({"ward": mode})
    # Unknown components and undefined modes are rejected, never absorbed.
    with pytest.raises(ValueError):
        validate_component_preference({"evil": "off"})
    with pytest.raises(ValueError):
        validate_component_preference({"owl": "mandatory"})
    # Case-variant smuggling still hits the floor check.
    with pytest.raises(PermissionError):
        validate_component_preference({"WARD": "off"})
    # The catalog is untouched by every forged attempt.
    assert sorted(COMPONENT_ALLOWED_MODES["ward"]) == ["mandatory"]
    assert COMPONENT_BASELINE_MODES["ward"] == "mandatory"


def test_non_dynamic_controls_stay_pinned():
    pinned = [component_id for component_id in COMPONENT_IDS
              if not COMPONENT_DYNAMIC_CONTROL[component_id]]
    assert pinned, "expected at least one non-dynamic control"
    for component_id in pinned:
        baseline = COMPONENT_BASELINE_MODES[component_id]
        assert validate_component_preference({component_id: baseline}) == {
            component_id: baseline}
        for mode in sorted(COMPONENT_ALLOWED_MODES[component_id]):
            if mode != baseline:
                with pytest.raises(PermissionError):
                    validate_component_preference({component_id: mode})


def test_valid_preference_returns_normalized_copy():
    result = validate_component_preference(
        {"owl": "elevated", "ward": "mandatory"})
    assert result == {"owl": "elevated", "ward": "mandatory"}
    assert type(result) is dict
    result["owl"] = "off"
    assert COMPONENT_BASELINE_MODES["owl"] == "normal"


def test_component_control_enforces_floor_locally():
    # The dataclass boundary itself rejects a lowered floor without any
    # adapter involvement: validation lives in component_control.
    with pytest.raises((ValueError, PermissionError)):
        ComponentControl(component_id="ward", mode="off", scope=None,
                         expires_at_action=None, source_revision=0)
    with pytest.raises((ValueError, PermissionError)):
        ComponentControl(component_id="ward", mode="passive", scope=None,
                         expires_at_action=None, source_revision=0)
    pinned = ComponentControl(component_id="ward", mode="mandatory",
                              scope=None, expires_at_action=None,
                              source_revision=0)
    assert pinned.mode == "mandatory"


# --- host-facing adapter snapshot ------------------------------------------

def test_adapter_capability_snapshot_single_bounded_call():
    adapter = _fake_adapter(revision=7)
    snapshot = adapter.capability_snapshot()
    assert set(snapshot["components"]) == set(COMPONENT_IDS)
    assert snapshot["behavior_revision"] == 7
    assert snapshot["lifecycle_revision"] == 7
    assert snapshot["authority_id"].startswith("cfw-execution:")
    assert snapshot["adapter_kind"] == "cfw-component-runtime"
    assert snapshot["components"]["ward"]["modifiable"] is False
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    assert len(encoded.encode("utf-8")) <= MAX_CAPABILITY_SNAPSHOT_BYTES
    # Cache-and-compare: stable within a revision, hash-busted on change.
    assert adapter.capability_snapshot() == snapshot
    adapter._session.external_behavior_revision = 8
    moved = adapter.capability_snapshot()
    assert moved["capability_hash"] != snapshot["capability_hash"]
    assert moved["lifecycle_revision"] == 8


def test_adapter_forged_framework_directive_fail_closed():
    adapter = _fake_adapter()
    scope = adapter._session._dp_scope

    def directive(target: str, value: str) -> dict:
        return {"directive_id": "d1", "operation": "set_framework",
                "target": target, "value": value, "cause": "c",
                "expected_effect": "e", "scope": scope,
                "expires_after_actions": 5, "intervention_id": "i1",
                "expires_at_action": None}

    with pytest.raises(PermissionError):
        adapter._validate_directive(directive("ward", "off"))
    with pytest.raises(ValueError):
        adapter._validate_directive(directive("evil", "off"))
    with pytest.raises(ValueError):
        adapter._validate_directive(directive("owl", "mandatory"))
    valid = adapter._validate_directive(directive("owl", "elevated"))
    assert valid["target"] == "owl" and valid["value"] == "elevated"
    assert sorted(COMPONENT_ALLOWED_MODES["ward"]) == ["mandatory"]
