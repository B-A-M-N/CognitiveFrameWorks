"""Benchmark preflight and sanity tripwires (Pass 4).

Each tripwire exists because a real cohort run got it wrong. These tests prove
the tripwire fires on that exact defect, so it can never again be mistaken for
an experimental result.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "cohort_preflight_under_test", ROOT / "scripts" / "cohort" / "preflight.py")
preflight = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = preflight
_spec.loader.exec_module(preflight)

PreflightError = preflight.PreflightError


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _response(answer: dict, **overrides):
    payload = {"answer": json.dumps(answer), "complete": True,
               "usage": {"input_tokens": 1, "output_tokens": 1}}
    payload.update(overrides)
    return payload


# -- envelope validation -------------------------------------------------

def test_missing_completion_flag_is_rejected():
    """The bug that scored all 70 trials as resource-limit failures."""
    response = _response({"status": "blocked"}, complete=False)
    with pytest.raises(PreflightError, match="completion flag"):
        preflight.validate_agent_response(response, required_fields=("status",))


def test_nested_answer_object_is_rejected():
    """Evaluators call json.loads() on the answer; an object cannot be parsed."""
    response = {"answer": {"status": "blocked"}, "complete": True}
    with pytest.raises(PreflightError, match="JSON string"):
        preflight.validate_agent_response(response, required_fields=("status",))


def test_answer_omitting_an_evaluated_field_is_rejected():
    response = _response({"status": "blocked"})
    with pytest.raises(PreflightError, match="omits evaluated fields"):
        preflight.validate_agent_response(response,
                                          required_fields=("status", "inspected"))


def test_unparsable_answer_is_rejected():
    response = {"answer": "not json at all", "complete": True}
    with pytest.raises(PreflightError, match="not valid JSON"):
        preflight.validate_agent_response(response, required_fields=("status",))


def test_lost_workspace_is_rejected(tmp_path):
    response = _response({"status": "ok"})
    with pytest.raises(PreflightError, match="removed or lost the task workspace"):
        preflight.validate_agent_response(response, required_fields=("status",),
                                          workspace=str(tmp_path / "gone"))


def test_task_envelope_requires_a_usable_instruction():
    with pytest.raises(PreflightError, match="missing a usable instruction"):
        preflight.validate_task_envelope({"task_family": "code_repair", "instruction": ""})
    with pytest.raises(PreflightError, match="missing a usable task_family"):
        preflight.validate_task_envelope({"instruction": "do the thing"})


# -- evaluator contracts derive from real evaluator source ---------------

def test_evaluator_contract_is_derived_from_the_evaluator():
    required = preflight.validate_evaluator_contract(
        "statework_transition", "statework_transition.py")
    # These are exactly the fields that evaluator reads.
    assert {"from", "to", "expected_revision", "stale_conflict_handled"} <= required


def test_adapter_specs_cover_every_evaluator_contract():
    """The adapter must ask for every field the evaluator scores.

    This is the check that would have caught the four families that reported
    0.00 in every cohort before any inference was spent.
    """
    adapter = importlib.util.spec_from_file_location(
        "preflight_adapter_check", ROOT / "scripts" / "cohort" / "agent_codex.py")
    module = importlib.util.module_from_spec(adapter)
    sys.modules[adapter.name] = module
    adapter.loader.exec_module(module)

    contract = json.loads(
        (ROOT / "contracts" / "real-agent-benchmark-v1.json").read_text())
    for family in contract["task_families"]:
        family_id = str(family["id"])
        required = preflight.validate_evaluator_contract(
            family_id, str(family["task_file"]))
        spec = module._fields_for({"task_family": family_id})
        missing = sorted(field for field in required if field not in spec)
        assert not missing, f"{family_id} spec omits {missing}"


def test_canary_uses_multi_turn_agent_runner_for_host_action_roundtrip(
        tmp_path, monkeypatch):
    class FakeBenchmark:
        @staticmethod
        def run_agent(command, task, **limits):
            assert limits["max_model_calls"] == 6
            assert task["task_id"] == "canary-code_repair-0"
            (Path(task["workspace"]) / "fixed.py").write_text(
                "def add(a, b):\n    return a + b\n")
            return ([
                {"turn": 0, "response": {"complete": False,
                                          "host_action_receipts": [{"executed": True}]}},
                {"turn": 1, "response": _response({"status": "fixed"})},
            ], {"model_calls": 2, "external_tokens": 2, "monetary_cost": 0.0})

    monkeypatch.setattr(preflight, "_load_benchmark", lambda: FakeBenchmark)
    result = preflight.run_canaries(
        agent_command=("agent-host",),
        families=({"id": "code_repair", "task_file": "code_repair.py"},),
        limits={"max_elapsed_seconds_per_trial": 180,
                "max_model_calls_per_trial": 6,
                "max_external_tokens_per_trial": 20_000,
                "max_monetary_cost_per_trial": 1.0},
    )

    assert result == [{"task_family": "code_repair", "trial": 0,
                       "attempt": 1, "required_fields": [],
                       "completion_success": True}]


# -- post-matrix tripwires ----------------------------------------------

def test_hard_zero_family_aborts_the_matrix():
    """A family at exactly zero in every cell is a harness defect."""
    results = [
        {"task_family": "code_repair", "completion_success": True},
        {"task_family": "code_repair", "completion_success": True},
        {"task_family": "long_running_recovery", "completion_success": False},
        {"task_family": "long_running_recovery", "completion_success": False},
    ]
    with pytest.raises(PreflightError, match="scored zero in every configuration"):
        preflight.check_hard_zero(results)


def test_a_genuinely_varying_family_does_not_trip_the_hard_zero_guard():
    results = [
        {"task_family": "code_repair", "completion_success": True},
        {"task_family": "code_repair", "completion_success": False},
    ]
    preflight.check_hard_zero(results)


def test_total_budget_burn_aborts_the_matrix():
    """Every trial hitting the limit means the handshake is broken."""
    results = [{"failure_category": "resource_limit"} for _ in range(5)]
    with pytest.raises(PreflightError, match="exhausted the action budget"):
        preflight.check_full_budget_burn(results)


def test_normal_failures_do_not_trip_the_budget_guard():
    results = [{"failure_category": "illegal_transition"},
               {"failure_category": None}]
    preflight.check_full_budget_burn(results)


# -- semantic treatment attestation (P1-16) ------------------------------

def _valid_manifest():
    return {"semantic_provider_status": "enabled",
            "backend_id": "digitalpsychology-laya-native",
            "model_id": "m", "model_revision": "r",
            "model_content_hash": "a" * 64, "provider_protocol_hash": "b" * 64,
            "provider_mode": "shadow", "capability_digest": "d" * 64,
            "calibration_bundle_hash": "c" * 64,
            "enabled_question_families": ["action_relation"],
            "capability_handshake_observed": True}


def test_laya_arm_with_disabled_provider_is_an_invalid_treatment():
    manifest = _valid_manifest()
    manifest["semantic_provider_status"] = "disabled"
    with pytest.raises(PreflightError, match="INVALID_TREATMENT"):
        preflight.check_semantic_treatment({"cohort-E": manifest})


@pytest.mark.parametrize("missing", [
    "backend_id", "model_id", "model_revision", "model_content_hash",
    "provider_protocol_hash", "calibration_bundle_hash", "provider_mode",
    "capability_digest", "enabled_question_families"])
def test_laya_arm_missing_attestation_is_rejected(missing):
    manifest = _valid_manifest()
    del manifest[missing]
    with pytest.raises(PreflightError, match="missing provider attestation"):
        preflight.check_semantic_treatment({"cohort-E": manifest})


def test_laya_arm_without_a_capability_handshake_is_rejected():
    manifest = _valid_manifest()
    manifest["capability_handshake_observed"] = False
    with pytest.raises(PreflightError, match="capability handshake"):
        preflight.check_semantic_treatment({"cohort-E": manifest})


def test_fully_attested_laya_arm_passes():
    preflight.check_semantic_treatment({"cohort-E": _valid_manifest()})


def test_laya_treatment_rejects_unpinned_or_disabled_provider_metadata():
    manifest = _valid_manifest()
    manifest["provider_mode"] = "disabled"
    with pytest.raises(PreflightError, match="invalid semantic provider mode"):
        preflight.check_semantic_treatment({"cohort-E": manifest})

    manifest = _valid_manifest()
    manifest["capability_digest"] = "unhashed"
    with pytest.raises(PreflightError, match="invalid provider attestation digest"):
        preflight.check_semantic_treatment({"cohort-E": manifest})


# -- telemetry presence and the fairness invariant ------------------------

def test_missing_telemetry_aborts_before_inference():
    reports = {"cohort-A": {"committed_actions": 3}}
    with pytest.raises(PreflightError, match="missing behavioral telemetry"):
        preflight.check_telemetry_present(reports)


def test_complete_telemetry_passes():
    reports = {"cohort-A": {"committed_actions": 3, "max_starvation_age": 1,
                            "fairness_bound": 4, "fairness_violation_count": 0,
                            "mode_transitions": 1, "rapid_mode_reversals": 0}}
    preflight.check_telemetry_present(reports)


def test_a_fairness_violation_is_reported_not_hidden():
    records = {"cohort-D": {"fairness_violation_count": 2}}
    with pytest.raises(PreflightError, match="fairness violations"):
        preflight.assert_triads_agree(records)


def test_zero_violations_passes_the_production_invariant():
    preflight.assert_triads_agree({"cohort-D": {"fairness_violation_count": 0}})


# -- infrastructure failures are not behavioral evidence ------------------

def test_hard_zero_ignores_infrastructure_failures():
    """A flaky backend must not be scored as the agent failing the task."""
    results = [
        {"task_family": "long_running_recovery", "completion_success": False,
         "infrastructure_failure": True},
        {"task_family": "long_running_recovery", "completion_success": False,
         "infrastructure_failure": True},
        {"task_family": "long_running_recovery", "completion_success": True},
    ]
    preflight.check_hard_zero(results)


def test_a_family_with_only_infrastructure_failures_is_still_rejected():
    """All-infra means there is no measurement at all, which is also unusable."""
    results = [
        {"task_family": "code_repair", "completion_success": False,
         "infrastructure_failure": True},
        {"task_family": "code_repair", "completion_success": False,
         "infrastructure_failure": True},
    ]
    with pytest.raises(PreflightError, match="only infrastructure failures"):
        preflight.check_hard_zero(results)


def test_a_genuine_hard_zero_still_aborts():
    """Infrastructure tolerance must not weaken the real defect check."""
    results = [
        {"task_family": "code_repair", "completion_success": False},
        {"task_family": "code_repair", "completion_success": False},
    ]
    with pytest.raises(PreflightError, match="scored zero in every"):
        preflight.check_hard_zero(results)


def test_resource_limit_rows_count_as_infrastructure():
    results = [{"task_family": "code_repair", "completion_success": False,
                "failure_category": "resource_limit"},
               {"task_family": "code_repair", "completion_success": True}]
    preflight.check_hard_zero(results)


def test_adapter_marks_an_empty_reply_as_infrastructure():
    adapter = _load("cohort_adapter_infrastructure", ROOT / "scripts" / "cohort" / "agent_codex.py")
    with pytest.raises(ValueError, match="no JSONL events"):
        adapter.parse_codex_jsonl("")
    response = adapter._infrastructure_failure(RuntimeError("Codex returned no output"))
    assert response["infrastructure_failure"] is True
    assert response["usage_source"] == "missing"
