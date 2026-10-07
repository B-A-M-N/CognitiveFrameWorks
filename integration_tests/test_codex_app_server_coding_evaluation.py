from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integration_tests"))

from codex_eval.behavior_scenarios import SCENARIOS, prepare_behavior_scenario
from codex_eval.trace_analysis import (
    anti_hyperfocus_metrics,
    bounded_payload_sha256,
    compare_behavior_trace_outcomes,
)
from codex_eval.validators import _validator_definitions
from codex_eval.report import _sanitized_trial_summary
from run_codex_app_server_coding_evaluation import (
    _summarize,
    _trial_result_category,
)
from cognitive_runtime_core import EvaluationCohortIdentity


def _cohort_identity(**changes):
    values = {
        "harness_id": "codex-app-server",
        "harness_version": "codex-cli 0.160.0",
        "adapter_version": "0.2.0",
        "adapter_hash": "a" * 64,
        "agent_model": "gpt-6-luna",
        "model_revision": "unavailable",
        "provider": "openai",
        "provider_endpoint_digest": "not_applicable",
        "auth_mode": "chatgpt",
        "harness_runtime_version": "codex-cli 0.160.0",
        "feature_catalog_hash": "b" * 64,
        "reasoning_effort": "default_unspecified",
        "task_fixture_hash": "c" * 64,
        "task_contract_hash": "d" * 64,
        "tool_binding_hash": "e" * 64,
        "validator_binding_hash": "f" * 64,
        "cfw_version": "1.4.0",
        "cfw_source_hash": "1" * 64,
        "csw_version": "1.0.0",
        "csw_source_hash": "2" * 64,
        "dp_version": "1.0.0",
        "dp_source_hash": "3" * 64,
        "laya_mode": "disabled",
        "laya_model_id": "not_applicable",
        "laya_calibration_hash": "not_applicable",
    }
    values.update(changes)
    return EvaluationCohortIdentity(**values).to_dict()


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
         "evaluation_cohort_identity": _cohort_identity(),
         "inference_ready": True, "model_execution_observed": True,
         "host_integration_valid": True, "infrastructure_valid": True,
         "efficacy_scored": True, "efficacy_valid": True,
         "completion_applicable": False, "completion_permitted": None,
         "criteria_passed": 2, "turn_status": "completed"},
        {"condition": "triad", "pair_id": "pair-01",
         "evaluation_cohort_identity": _cohort_identity(),
         "inference_ready": True, "model_execution_observed": True,
         "host_integration_valid": True, "infrastructure_valid": True,
         "efficacy_scored": True, "efficacy_valid": True,
         "completion_applicable": True, "completion_permitted": True,
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
    assert summary["conditions"]["baseline"]["completion_applicable_trials"] == 0
    assert summary["conditions"]["triad"]["completion_applicable_trials"] == 2
    assert summary["conditions"]["triad"]["completion_observed_trials"] == 1
    assert summary["matched_pairs"]["triad"][
        "pairs_with_both_efficacy_valid"] == 1
    assert summary["matched_pairs"]["triad"][
        "mean_criteria_delta_vs_baseline"] == 2.0
    assert summary["matched_pairs"]["triad"]["cohort_pair_statuses"] == {
        "same_cohort": 1, "incomparable_cohort": 0,
        "missing_cohort_identity": 0, "invalid_cohort_identity": 0,
        "insufficient_evidence": 0}


def test_matched_efficacy_pair_is_excluded_when_cohort_hashes_differ():
    rows = [
        {"condition": "baseline", "pair_id": "pair-01",
         "evaluation_cohort_identity": _cohort_identity(),
         "efficacy_valid": True, "criteria_passed": 2},
        {"condition": "triad", "pair_id": "pair-01",
         "evaluation_cohort_identity": _cohort_identity(laya_mode="advisory"),
         "efficacy_valid": True, "criteria_passed": 4},
    ]
    summary = _summarize(rows, [{"pair_id": "pair-01"}],
                         ("baseline", "triad"))["matched_pairs"]["triad"]

    assert summary["pairs_with_both_efficacy_valid_before_cohort_check"] == 1
    assert summary["pairs_with_both_efficacy_valid"] == 0
    assert summary["mean_criteria_delta_vs_baseline"] is None
    assert summary["cohort_pair_statuses"]["incomparable_cohort"] == 1
    assert summary["comparison_result_categories"]["INCOMPARABLE_COHORT"] == 1


def test_matched_efficacy_pair_requires_a_valid_cohort_identity():
    rows = [
        {"condition": "baseline", "pair_id": "pair-01",
         "efficacy_valid": True, "criteria_passed": 2},
        {"condition": "triad", "pair_id": "pair-01",
         "efficacy_valid": True, "criteria_passed": 4},
    ]
    summary = _summarize(rows, [{"pair_id": "pair-01"}],
                         ("baseline", "triad"))["matched_pairs"]["triad"]

    assert summary["pairs_with_both_efficacy_valid"] == 0
    assert summary["cohort_pair_statuses"]["missing_cohort_identity"] == 1
    assert summary["comparison_result_categories"]["INSUFFICIENT_EVIDENCE"] == 1


def test_trial_result_categories_keep_blockers_separate_from_task_failure():
    assert _trial_result_category({
        "efficacy_valid": True, "criteria_passed": 4,
        "criteria_total": 4}) == "PASS"
    assert _trial_result_category({
        "efficacy_valid": True, "criteria_passed": 2,
        "criteria_total": 4}) == "FAIL"
    assert _trial_result_category({
        "failure_class": "capability_unavailable"}) == "CAPABILITY_BLOCKED"
    assert _trial_result_category({
        "upstream_blocked": True}) == "INFRASTRUCTURE_BLOCKED"
    assert _trial_result_category({
        "model_execution_observed": True, "efficacy_valid": False
    }) == "INVALID_TREATMENT"
    assert _trial_result_category({}) == "INSUFFICIENT_EVIDENCE"


def test_trial_sanitizer_preserves_only_valid_cohort_identity():
    identity = _cohort_identity()
    observed = _sanitized_trial_summary({
        "condition": "triad", "evaluation_cohort_identity": identity})
    invalid = dict(identity, cohort_hash="0" * 64)
    rejected = _sanitized_trial_summary({
        "condition": "triad", "evaluation_cohort_identity": invalid})

    assert observed["evaluation_cohort_identity"] == identity
    assert "evaluation_cohort_identity" not in rejected


def test_baseline_completion_is_explicitly_not_applicable():
    row = _sanitized_trial_summary({
        "condition": "baseline",
    })

    assert row["completion_applicable"] is False
    assert row["completion_status"] == "not_applicable"
    assert row["completion_decision"] == "not_applicable"
    assert row["completion_attempts"] == []
    assert row["completion_permitted"] is None
    summary = _summarize([row], [], ("baseline",))["conditions"]["baseline"]
    assert summary["completion_applicable_trials"] == 0
    assert summary["completion_permits"] is None
    assert summary["completion_denials_or_incomplete"] is None


def test_evaluation_accepts_legacy_production_counters_alias():
    rows = [{
        "condition": "triad", "efficacy_valid": True,
        "runtime_harness_lifecycle_status": {
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


def test_report_separates_model_validator_calls_from_host_validator_runs():
    row = _sanitized_trial_summary({
        "condition": "triad", "efficacy_valid": True,
        "completion_applicable": True, "completion_permitted": True,
        "criteria_passed": 4,
        "action_calls": [
            {"tool": "cfw_read", "call_id": "call/read-1", "success": True,
             "arguments": {"path": "private/source.py"}},
            {"tool": "cfw_run_validator", "call_id": "call/validate-1",
             "validator": "parser", "success": False,
             "arguments": {"validator": "parser", "secret": "omit-me"}},
        ],
        "server_request_method_counts": {
            "item/tool/call": 2, "turn/start": 1},
        "completion_validators": [{
            "validator_id": "parser", "criterion_ids": ["parser_correctness"],
            "passed": True}],
        "completion_attempts": [{
            "attempt": 1, "status": "incomplete", "decision": "deny",
            "validators": [{
                "validator_id": "parser",
                "criterion_ids": ["parser_correctness"], "passed": False}],
        }],
        "behavior_action_trace": [{
            "tool": "cfw_run_validator", "result_class": "failure",
            "cfw_gate_stage": "final", "cfw_gate_decision": "allow",
        }],
        "cfw_gate_receipts": [{
            "stage": "final", "decision": "allow",
            "invocation_id": "call/validate-1", "reason_codes": [],
        }],
        "integration_invariants": {"cfw_action_gate_observed": False},
        "validator_channel_metrics": {
            "model_turn": {"runs": 1, "passed": 0, "failed": 1,
                            "total_elapsed_ms": 25.0},
            "host_completion": {"runs": 2, "passed": 1, "failed": 1,
                                "total_elapsed_ms": 80.0},
        },
    })

    assert row["model_tool_call_count"] == 2
    assert row["model_validator_call_count"] == 1
    assert row["server_request_method_counts"] == {
        "item/tool/call": 2, "turn/start": 1}
    assert row["completion_validators"][0]["criterion_ids"] == [
        "parser_correctness"]
    assert row["completion_attempts"][0]["validators"][0]["passed"] is False
    assert row["integration_invariants"]["cfw_action_gate_observed"] is True
    assert "arguments" not in row["action_calls"][1]

    # The parent runner adds efficacy validity after parsing the child's
    # sanitized trial record.
    summary = _summarize(
        [{**row, "efficacy_valid": True}], [], ("triad",)
    )["conditions"]["triad"]
    assert summary["model_tool_calls_distribution"]["mean"] == 2
    assert summary["model_validator_calls_distribution"]["mean"] == 1
    assert summary["model_tool_call_failures"] == 1
    assert summary["validator_channel_metrics"]["model_turn"]["runs"] == 1
    assert summary["validator_channel_metrics"]["host_completion"]["runs"] == 2


def test_cfw_gate_observation_is_required_in_every_cfw_condition():
    for condition in ("cfw", "cfw-csw", "triad", "triad-multipath"):
        missing = _sanitized_trial_summary({
            "condition": condition,
            "integration_invariants": {"cfw_action_gate_observed": True},
        })
        assert missing["integration_invariants"]["cfw_action_gate_observed"] is False

        observed = _sanitized_trial_summary({
            "condition": condition,
            "behavior_action_trace": [{"tool": "cfw_read"}],
            "cfw_gate_receipts": [{"stage": "final", "decision": "allow"}],
        })
        assert observed["integration_invariants"]["cfw_action_gate_observed"] is True


def test_evaluation_aggregates_canonical_and_legacy_dp_metrics_as_distributions():
    first = _sanitized_trial_summary({
        "condition": "triad", "efficacy_valid": True,
        "criteria_passed": 4,
        "runtime_harness_lifecycle_status": {
            "dp_intervention_count": 2,
            "dp_intervention_metrics": {
                "unique_created": 2, "active": 1, "applications": 1,
                "closed": 1, "effective": 1, "ineffective": 0, "unknown": 0,
                "created_by_origin": {
                    "startup": 1, "runtime_behavior": 1,
                    "post_action": 0, "completion": 0}},
            "production_metrics": {
                "production_counters": {"control_ticks_committed": 2},
                "counters": {"control_ticks_committed": 3,
                              "reconciliations": 1}},
        },
    })
    second = _sanitized_trial_summary({
        "condition": "triad", "efficacy_valid": True,
        "criteria_passed": 2,
        "runtime_harness_lifecycle_status": {
            "dp_intervention_count": 4,
            "dp_intervention_metrics": {
                "unique_created": 4, "active": 1, "applications": 3,
                "closed": 3, "effective": 2, "ineffective": 1, "unknown": 0,
                "created_by_origin": {
                    "startup": 1, "runtime_behavior": 1,
                    "post_action": 1, "completion": 1}},
            "production_metrics": {
                "production_counters": {"control_ticks_committed": 5,
                                        "reconciliations": 0}},
        },
    })

    summary = _summarize(
        [{**first, "efficacy_valid": True},
         {**second, "efficacy_valid": True}], [], ("triad",)
    )["conditions"]["triad"]

    assert summary["criteria_passed_distribution"] == {
        "count": 2, "mean": 3.0, "median": 3.0, "min": 2.0, "max": 4.0}
    assert summary["dp_intervention_metric_totals"]["unique_created"] == 6
    assert summary["dp_intervention_creations_by_origin"] == {
        "startup": 2, "runtime_behavior": 2,
        "post_action": 1, "completion": 1}
    assert summary["dp_intervention_origin_accounting_valid_trials"] == 2
    assert summary["dp_control_ticks_committed"] == 8
    assert summary["dp_counter_distributions"]["control_ticks_committed"] == {
        "count": 2, "mean": 4.0, "median": 4.0, "min": 3.0, "max": 5.0}


def test_behavior_trace_analysis_compares_bounded_success_and_failure_signals():
    traces = [
        {"criteria_passed": 4, "behavior_trace": [
            {"behavioral_mode": "DELIVER", "mode_transition": "initial",
             "component_modes": {"owl": "normal", "ward": "normal"},
             "focus_criterion_ids": ["C1"], "guidance_sha256": "a" * 64,
             "guidance_byte_count": 100, "model_tool_call_count": 3,
             "native_write_request_count": 0, "completion_attempt_count": 1,
             "completion_validator_order": ["parser", "state"],
             "retry_recovery_counters": {"same_approach_retries": 0}},
            {"behavioral_mode": "VERIFY", "mode_transition": "DELIVER:VERIFY",
             "component_modes": {"owl": "passive", "ward": "normal"},
             "focus_criterion_ids": ["C2"], "guidance_sha256": "b" * 64,
             "guidance_byte_count": 120, "model_tool_call_count": 2,
             "native_write_request_count": 0, "completion_attempt_count": 1,
             "completion_validator_order": ["parser", "state"],
             "retry_recovery_counters": {"same_approach_retries": 0}},
        ]},
        {"criteria_passed": 2, "behavior_trace": [
            {"behavioral_mode": "REPLAN", "mode_transition": "initial",
             "component_modes": {"owl": "recovery"},
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
    assert summary["cohorts"]["complete_4_of_4"][
        "component_mode_turn_counts"] == {
            "owl:normal": 1, "owl:passive": 1, "ward:normal": 2}
    assert summary["cohorts"]["below_4_of_4"][
        "component_mode_turn_counts"] == {"owl:recovery": 1}
    assert summary["complete_minus_below_mean_differences"][
        "model_tool_call_count"] == 4
    assert "does not identify causation" in summary["interpretation"]


def test_component_modes_are_sanitized_in_bounded_behavior_trace():
    row = _sanitized_trial_summary({"condition": "cfw", "behavior_trace": [{
        "turn_index": 1,
        "component_modes": {
            "owl": "normal", "ward": "strict", "bad mode": "invalid",
            "oversized": "x" * 65,
        },
    }]})

    assert row["behavior_trace"][0]["component_modes"] == {
        "owl": "normal", "ward": "strict"}


def test_action_payload_trace_keeps_only_bounded_digests():
    digest = bounded_payload_sha256({"path": "/private/secret.py",
                                     "content": "secret source text"})
    assert isinstance(digest, str) and len(digest) == 64
    assert "secret" not in digest
    assert bounded_payload_sha256({"content": "x" * 65_536}) is None
    assert bounded_payload_sha256(object()) is None


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


def test_missing_retry_observations_remain_unavailable_in_trace_metrics():
    row = {
        "criteria_passed": 4,
        "required_criterion_ids": ["C1"],
        "behavior_action_trace": [{
            "target_criterion_id": "C1", "action_class": "read",
            "result_class": "success",
        }],
    }

    metrics = anti_hyperfocus_metrics(row)
    summary = compare_behavior_trace_outcomes([row])

    assert metrics["same_approach_retries"] is None
    assert metrics["equivalent_retries"] is None
    cohort = summary["cohorts"]["complete_4_of_4"]
    assert cohort["same_approach_retry_distribution"]["count"] == 0
    assert cohort["anti_hyperfocus"]["equivalent_retry_distribution"]["count"] == 0


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
        "multitask-starvation", "equivalent-retry", "goal-drift",
        "premature-completion", "ambiguous-progress", "scope-drift",
        "stale-authority", "stale-evidence", "workflow-switch", "context-pressure",
    }
    manifest = json.loads((ROOT / "evaluation_fixtures" / "behavior-headroom-v1"
                           / "manifest.json").read_text(encoding="utf-8"))
    assert {item["id"] for item in manifest["cases"]} == set(SCENARIOS)
    pilot = manifest["observed_pilot_distribution"]
    assert pilot["defined_scenario_count"] == len(SCENARIOS)
    assert pilot["evaluated_scenario_count"] == 7
    assert set(pilot["unevaluated_scenarios"]) == {
        "goal-drift", "ambiguous-progress", "stale-evidence"}
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

    retry = prepare_behavior_scenario(tmp_path, prompt, "equivalent-retry")
    assert "exactly twice" in retry["task_prompt"]
    assert "unchanged source" in retry["task_prompt"]
    assert "second identical result is not new evidence" in retry["task_prompt"]

    drift = prepare_behavior_scenario(tmp_path, prompt, "goal-drift")
    assert drift["task_prompt"].startswith(prompt["task_prompt"])
    assert "original host-owned objective" in drift["task_prompt"]
    assert "Future roadmap" in (tmp_path / "PROJECT_ROADMAP.md").read_text(
        encoding="utf-8")

    ambiguous = prepare_behavior_scenario(tmp_path, prompt, "ambiguous-progress")
    smoke_report = (tmp_path / "SMOKE_REPORT.txt").read_text(encoding="utf-8")
    assert "criterion-specific" in ambiguous["task_prompt"]
    assert "parser_correctness only" in smoke_report
    assert "not measured" in smoke_report

    stale_evidence = prepare_behavior_scenario(tmp_path, prompt, "stale-evidence")
    cached_report = json.loads(
        (tmp_path / "CACHED_VALIDATION_REPORT.json").read_text(encoding="utf-8"))
    assert "earlier source revision" in stale_evidence["task_prompt"]
    assert cached_report["revision"] == "earlier-source-snapshot"
    assert len(cached_report["source_digest"]) == 64
    assert set(cached_report["source_digest"]) == {"0"}
