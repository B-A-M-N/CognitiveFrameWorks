"""The cohort must construct real configurations, not just set env vars.

The benchmark harness talks JSON to an agent subprocess. On its own it never
builds CFW, never binds DP, and therefore cannot observe starvation or fairness.
These tests prove the runtime harness constructs each composition for real and
that an unattainable treatment is refused rather than silently downgraded.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "cohort_runtime_harness_under_test",
    ROOT / "scripts" / "cohort" / "runtime_harness.py")
harness = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness
_spec.loader.exec_module(harness)


def _provider_manifest(**overrides):
    manifest = {"semantic_provider_status": "enabled", "backend_id": "laya",
                "model_id": "m", "model_revision": "r",
                "model_content_hash": "a" * 64, "provider_protocol_hash": "b" * 64,
                "calibration_bundle_hash": "c" * 64,
                "enabled_question_families": ["action_relation"],
                "capability_handshake_observed": True}
    manifest.update(overrides)
    return manifest


# -- configurations are really constructed --------------------------------

def test_baseline_builds_cfw_without_csw_or_dp(tmp_path):
    handles = harness.build_session(configuration="baseline", task_id="b-1",
                                    state_root=tmp_path)
    assert handles["cfw_session"] is not None
    assert handles["csw_authority"] is None
    assert handles["dp_session"] is None


def test_cfw_csw_builds_a_real_csw_authority(tmp_path):
    handles = harness.build_session(configuration="cfw_csw", task_id="c-1",
                                    state_root=tmp_path)
    assert handles["cfw_session"] is not None
    assert handles["csw_authority"] is not None
    assert handles["dp_session"] is None


def test_cfw_csw_dp_binds_a_real_dp_session(tmp_path):
    handles = harness.build_session(configuration="cfw_csw_dp", task_id="d-1",
                                    state_root=tmp_path)
    assert handles["dp_session"] is not None
    assert hasattr(handles["dp_session"], "behavioral_trial_record")


def test_an_unknown_configuration_is_refused(tmp_path):
    with pytest.raises(harness.HarnessError, match="unknown cohort configuration"):
        harness.build_session(configuration="cfw_only_maybe", task_id="x",
                              state_root=tmp_path)


# -- telemetry is real, or honestly absent -------------------------------

def test_dp_configuration_emits_a_real_behavioral_record(tmp_path):
    sys.path.insert(0, "/home/bamn/DigitalPsychology")
    from lib.runtime.contracts import RuntimeObservation

    handles = harness.build_session(configuration="cfw_csw_dp",
                                    task_id="telemetry-1", state_root=tmp_path)
    dp = handles["dp_session"]
    criterion = "telemetry-1-criterion"
    for index in (1, 2, 3):
        dp.process_action(RuntimeObservation(
            action_index=index, criterion_status={criterion: "unmet"}))

    record = harness.behavioral_record(handles, trial_id="trial-1",
                                       task_outcome_score=1.0, task_completed=True)
    assert record["trial_id"] == "trial-1"
    assert record["committed_actions"] == 3
    assert record["fairness_bound"] > 0
    # Attended every action, so no criterion may have starved.
    assert record["fairness_violation_count"] == 0
    assert record["required_criterion_coverage"] == [criterion]


def test_configuration_without_dp_reports_absent_telemetry(tmp_path):
    """Absent telemetry must be None, never a zeroed placeholder record."""
    handles = harness.build_session(configuration="baseline", task_id="b-2",
                                    state_root=tmp_path)
    assert harness.behavioral_record(handles, trial_id="t") is None


# -- treatment attestation (P1-16) ---------------------------------------

def test_laya_arm_without_a_live_provider_is_an_invalid_treatment(tmp_path):
    with pytest.raises(harness.InvalidTreatment, match="INVALID_TREATMENT"):
        harness.build_session(configuration="cfw_csw_dp_laya", task_id="l-1",
                              state_root=tmp_path)
    with pytest.raises(harness.InvalidTreatment, match="INVALID_TREATMENT"):
        harness.build_session(configuration="cfw_csw_dp_laya", task_id="l-2",
                              state_root=tmp_path,
                              provider_manifest=_provider_manifest(
                                  semantic_provider_status="disabled"))


@pytest.mark.parametrize("missing", [
    "backend_id", "model_id", "model_revision", "model_content_hash",
    "provider_protocol_hash", "calibration_bundle_hash",
    "enabled_question_families", "capability_handshake_observed"])
def test_laya_arm_requires_every_attestation_field(tmp_path, missing):
    manifest = _provider_manifest()
    manifest.pop(missing)
    with pytest.raises(harness.InvalidTreatment):
        harness.build_session(configuration="cfw_csw_dp_laya", task_id="l-3",
                              state_root=tmp_path, provider_manifest=manifest)


def test_laya_arm_with_full_attestation_constructs(tmp_path):
    handles = harness.build_session(
        configuration="cfw_csw_dp_laya", task_id="l-4", state_root=tmp_path,
        provider_manifest=_provider_manifest())
    assert handles["dp_session"] is not None
    assert handles["csw_authority"] is not None
