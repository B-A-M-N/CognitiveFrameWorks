from __future__ import annotations

import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integration_tests"))

from codex_eval.behavior_scenarios import SCENARIOS, prepare_behavior_scenario
from codex_eval.trace_analysis import (
    anti_hyperfocus_metrics,
    compare_behavior_trace_outcomes,
)
from codex_eval.validators import _validator_definitions
from run_codex_app_server_coding_evaluation import _summarize


def test_completion_validator_schema_accepts_measured_runtime_result():
    validators, _ = _validator_definitions({"criteria": [{
        "validator": "parser", "id": "parser-criterion"}]})
    result = {
        "status": "passed", "validator": "parser", "tests_passed": 4,
        "tests_failed": 0, "summary": "4 passed", "elapsed_ms": 15.25,
    }

    jsonschema.validate(result, validators[0].result_schema_value())


def test_evaluation_excludes_infrastructure_failures_from_efficacy_pairs():
    rows = [
        {"condition": "baseline", "pair_id": "pair-01",
         "inference_ready": True, "model_execution_observed": True,
         "host_integration_valid": True, "infrastructure_valid": True,
         "efficacy_scored": True, "efficacy_valid": True,
         "criteria_passed": 2, "turn_status": "completed"},
        {"condition": "triad", "pair_id": "pair-01",
         "inference_ready": True, "model_execution_observed": True,
         "host_integration_valid": True, "infrastructure_valid": True,
         "efficacy_scored": True, "efficacy_valid": True,
         "integration_valid": True, "criteria_passed": 4,
         "turn_status": "completed"},
        {"condition": "baseline", "pair_id": "pair-02",
         "inference_ready": False, "model_execution_observed": False,
         "host_integration_valid": False, "infrastructure_valid": False,
         "efficacy_scored": False, "efficacy_valid": False,
         "upstream_blocked": True, "failure_class": "workspace_routing_unavailable"},
        {"condition": "triad", "pair_id": "pair-02",
         "inference_ready": False, "model_execution_observed": False,
         "host_integration_valid": False, "infrastructure_valid": False,
         "efficacy_scored": False, "efficacy_valid": False,
         "upstream_blocked": True, "failure_class": "workspace_routing_unavailable"},
    ]
    pairs = [{"pair_id": "pair-01"}, {"pair_id": "pair-02"}]

    summary = _summarize(rows, pairs, ("baseline", "triad"))

    assert summary["conditions"]["triad"]["scheduled_trials"] == 2
    assert summary["conditions"]["triad"]["inference_ready_trials"] == 1
    assert summary["conditions"]["triad"]["model_started_trials"] == 1
    assert summary["conditions"]["triad"]["efficacy_valid_trials"] == 1
    assert summary["conditions"]["triad"]["upstream_blocked_trials"] == 1
    assert summary["matched_pairs"]["triad"][
        "pairs_with_both_efficacy_valid"] == 1
    assert summary["matched_pairs"]["triad"][
        "mean_criteria_delta_vs_baseline"] == 2.0


def test_evaluation_accepts_legacy_production_counters_alias():
    rows = [{
        "condition": "triad", "efficacy_valid": True,
        "cognitive_host_status": {
            "dp_intervention_metrics": {
                "unique_created": 17, "active": 2, "applications": 15,
                "closed": 13, "effective": 5, "ineffective": 3,
                "unknown": 5,
                "created_by_origin": {
                    "startup": 2, "runtime_behavior": 5,
                    "post_action": 7, "completion": 3},
            },
            "production_metrics": {
                "production_counters": {"control_ticks_committed": 17},
            },
        },
    }]

    summary = _summarize(rows, [], ("triad",))["conditions"]["triad"]

    assert summary["dp_control_ticks_committed"] == 17
    assert summary["dp_intervention_metric_totals"] == {
        "unique_created": 17, "active": 2, "applications": 15,
        "closed": 13, "effective": 5, "ineffective": 3, "unknown": 5}
    assert summary["dp_intervention_creations_by_origin"] == {
        "startup": 2, "runtime_behavior": 5,
        "post_action": 7, "completion": 3}
    assert summary["dp_intervention_origin_accounting_valid_trials"] == 1


def test_behavior_trace_analysis_compares_bounded_success_and_failure_signals():
    traces = [
        {"criteria_passed": 4, "behavior_trace": [
            {"behavioral_mode": "DELIVER", "mode_transition": "initial",
             "focus_criterion_ids": ["C1"], "guidance_sha256": "a" * 64,
             "guidance_byte_count": 100, "model_tool_call_count": 3,
             "native_write_request_count": 0, "completion_attempt_count": 1,
             "completion_validator_order": ["parser", "state"],
             "retry_recovery_counters": {"same_approach_retries": 0}},
            {"behavioral_mode": "VERIFY", "mode_transition": "DELIVER:VERIFY",
             "focus_criterion_ids": ["C2"], "guidance_sha256": "b" * 64,
             "guidance_byte_count": 120, "model_tool_call_count": 2,
             "native_write_request_count": 0, "completion_attempt_count": 1,
             "completion_validator_order": ["parser", "state"],
             "retry_recovery_counters": {"same_approach_retries": 0}},
        ]},
        {"criteria_passed": 2, "behavior_trace": [
            {"behavioral_mode": "REPLAN", "mode_transition": "initial",
             "focus_criterion_ids": ["C1"], "guidance_sha256": "c" * 64,
             "guidance_byte_count": 80, "model_tool_call_count": 1,
             "native_write_request_count": 1, "completion_attempt_count": 1,
             "redirected": True,
             "retry_recovery_counters": {"same_approach_retries": 2}},
        ]},
    ]

    summary = compare_behavior_trace_outcomes(traces)

    assert summary["cohorts"]["complete_4_of_4"]["trials"] == 1
    assert summary["cohorts"]["below_4_of_4"]["trials"] == 1
    assert summary["cohorts"]["complete_4_of_4"][
        "completion_validator_order_counts"] == {"parser > state": 2}
    assert summary["complete_minus_below_mean_differences"][
        "model_tool_call_count"] == 4
    assert "does not identify causation" in summary["interpretation"]


def test_anti_hyperfocus_metrics_track_starvation_retries_and_yield_events():
    row = {
        "criterion_results": {"C1": True, "C2": True, "C3": True, "C4": False},
        "behavior_action_trace": [
            {"target_criterion_id": "C1", "action_class": "read",
             "elapsed_ms": 100},
            {"target_criterion_id": "C1", "action_class": "validator",
             "elapsed_ms": 200, "result_class": "failure",
             "same_approach_retry_count": 3},
            {"target_criterion_id": "C1", "action_class": "validator",
             "elapsed_ms": 300, "result_class": "failure",
             "criterion_progress_delta": 0},
            {"target_criterion_id": "C2", "action_class": "write",
             "elapsed_ms": 400},
            {"target_criterion_id": "C3", "action_class": "validator",
             "elapsed_ms": 500, "equivalent_retry_count": 2},
        ],
        "behavior_trace": [
            {"local_yield_event": True, "global_blocked_event": False},
            {"local_yield_event": False, "global_blocked_event": False},
        ],
    }

    metrics = anti_hyperfocus_metrics(row)

    assert metrics["actions_per_criterion"] == {"C1": 3, "C2": 1, "C3": 1, "C4": 0}
    assert metrics["criterion_attribution_ratio"] == 1
    assert metrics["unattributed_action_count"] == 0
    assert metrics["maximum_consecutive_actions_on_one_criterion"] == 3
    assert metrics["maximum_criterion_starvation_age_actions"] == 5
    assert metrics["time_to_first_action_ms_by_criterion"] == {
        "C1": 100, "C2": 400, "C3": 500, "C4": None}
    assert metrics["criteria_never_touched"] == ["C4"]
    assert metrics["attention_rotations"] == 2
    assert metrics["same_approach_retries"] == 3
    assert metrics["equivalent_retries"] == 2
    assert metrics["equivalent_retry_proxy_count"] == 1
    assert metrics["local_yield_events"] == 1
    assert metrics["global_blocked_events"] == 0


def test_evaluation_summarizes_action_metrics_for_baseline_and_cfw():
    rows = [{
        "condition": condition, "efficacy_valid": True,
        "criteria_passed": 4, "criteria_total": 4,
        "required_criterion_ids": ["C1", "C2"],
        "criterion_results": {"C1": True, "C2": True},
        "behavior_action_trace": [
            {"target_criterion_id": "C1", "target_source": "test_path",
             "action_class": "read", "elapsed_ms": 10},
            {"target_criterion_id": "C2", "target_source": "validator_call",
             "action_class": "validator", "elapsed_ms": 20},
        ],
        "behavior_trace": [{
            "local_yield_event": False, "global_blocked_event": False}],
    } for condition in ("baseline", "cfw")]

    summary = _summarize(rows, [], ("baseline", "cfw"))["conditions"]

    for condition in ("baseline", "cfw"):
        metrics = summary[condition]["behavior_trace_outcomes"][
            "descriptive_comparison"]["cohorts"]["complete_4_of_4"][
                "anti_hyperfocus"]
        assert metrics["trials_with_action_trace"] == 1
        assert metrics["actions_per_criterion"] == {"C1": 1, "C2": 1}
        assert metrics["criterion_attribution_ratio_distribution"]["mean"] == 1


def test_behavior_headroom_scenarios_are_bounded_and_prepare_their_stimulus(tmp_path):
    assert set(SCENARIOS) == {
        "multitask-starvation", "equivalent-retry", "premature-completion",
        "scope-drift", "stale-authority", "workflow-switch", "context-pressure",
    }
    prompt = {"task_prompt": "Repair source under the host contract."}

    noisy = prepare_behavior_scenario(tmp_path, prompt, "context-pressure")
    diagnostics = (tmp_path / "NOISY_DIAGNOSTICS.txt").read_text(encoding="utf-8")
    assert noisy["behavioral_scenario"] == "context-pressure"
    assert "BEHAVIORAL CHALLENGE" in noisy["task_prompt"]
    assert len(diagnostics.splitlines()) == 512
    assert noisy["behavioral_followup"]

    stale = prepare_behavior_scenario(tmp_path, prompt, "stale-authority")
    assert stale["behavioral_scenario"] == "stale-authority"
    assert "historical" in (tmp_path / "README.md").read_text(
        encoding="utf-8").lower()
