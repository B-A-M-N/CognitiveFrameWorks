"""Contract tests for the production qualification cohort harness.

These run without a model. They pin the harness guarantees the
`real_agent_behavioral_cohort` gate depends on: blinded host-side cohort
labels, holdout separation, host-evaluator-only evidence, and explicit
deterministic, shadow, and advisory arms. They do not assert behavioral
outcomes, which only a real agent run can establish.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _cohort():
    return _load("cohort_runner", ROOT / "scripts" / "cohort" / "run_cohort.py")


def test_harness_defines_deterministic_shadow_and_explicit_advisory_arms():
    cohort = _cohort()
    names = [name for name, _ in cohort.CONFIGURATIONS]
    assert names == [
        "baseline", "cfw", "cfw_csw", "cfw_csw_dp", "cfw_csw_dp_laya",
        "cfw_csw_dp_laya_advisory",
    ]
    assert "cfw_csw_dp_laya_advisory" not in cohort.DEFAULT_CONFIGURATIONS
    assert cohort.LAYA_CONFIGURATIONS == {
        "cfw_csw_dp_laya", "cfw_csw_dp_laya_advisory",
    }
    # Baseline is the no-framework control; every later cell is a strict
    # superset of the one before it, so each step adds and never removes.
    baseline_name, baseline_subsystems = cohort.CONFIGURATIONS[0]
    assert baseline_name == "baseline"
    assert "CFW-off" in baseline_subsystems
    previous: set[str] = {"CFW"}
    for name, subsystems in cohort.CONFIGURATIONS[1:]:
        current = set(subsystems)
        assert previous <= current, f"{name} drops {previous - current}"
        assert "CFW-off" not in current
        previous = current


def test_laya_cohort_requires_explicit_durable_evidence_directory(tmp_path: Path):
    cohort = _cohort()
    with pytest.raises(ValueError, match="require --evidence-dir"):
        cohort._require_durable_laya_evidence(["cfw_csw_dp_laya"], None)
    with pytest.raises(ValueError, match="require --evidence-dir"):
        cohort._require_durable_laya_evidence(
            ["cfw_csw_dp_laya_advisory"], None)
    cohort._require_durable_laya_evidence(["cfw_csw_dp"], None)
    cohort._require_durable_laya_evidence(["cfw_csw_dp_laya"], tmp_path)


def test_advisory_arm_requires_qualified_bundle_and_experiment_id(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cohort = _cohort()
    calibration = tmp_path / "calibration.json"
    calibration.write_text("{}", encoding="utf-8")
    environment = {
        "DIGITALPSYCHOLOGY_SYSTEM_ONE_CALIBRATION_PATH": str(calibration),
        "DIGITALPSYCHOLOGY_SYSTEM_ONE_ACTUATION_EXPERIMENT_ID": "bounded-study-1",
    }

    def qualified_bundle(args, **kwargs):
        assert kwargs["cwd"] == cohort.cohort_runtime.DP
        assert kwargs["env"] == environment
        compile(args[2], "advisory-calibration-preflight", "exec")
        return type("Completed", (), {
            "returncode": 0, "stdout": "qualified\n", "stderr": "",
        })()

    monkeypatch.setattr(cohort.subprocess, "run", qualified_bundle)
    cohort._require_advisory_inputs(
        ["cfw_csw_dp_laya_advisory"], environment)

    def ineligible_bundle(_args, **_kwargs):
        return type("Completed", (), {
            "returncode": 0, "stdout": "ineligible\n", "stderr": "",
        })()

    monkeypatch.setattr(cohort.subprocess, "run", ineligible_bundle)
    with pytest.raises(ValueError, match="active, qualified"):
        cohort._require_advisory_inputs(
            ["cfw_csw_dp_laya_advisory"], environment)


def test_advisory_calibration_gate_runs_before_canaries_or_agent_calls(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cohort = _cohort()
    monkeypatch.delenv("DIGITALPSYCHOLOGY_SYSTEM_ONE_CALIBRATION_PATH", raising=False)
    monkeypatch.delenv(
        "DIGITALPSYCHOLOGY_SYSTEM_ONE_ACTUATION_EXPERIMENT_ID", raising=False)
    monkeypatch.setattr(cohort.cohort_preflight, "run_canaries", lambda **_kwargs: (
        pytest.fail("advisory calibration must be checked before model canaries")))
    monkeypatch.setattr(sys, "argv", [
        "run_cohort.py", "--configurations", "cfw_csw_dp_laya_advisory",
        "--trials-per-cell", "1", "--evidence-dir", str(tmp_path / "evidence"),
    ])
    with pytest.raises(ValueError, match="existing DP calibration bundle"):
        cohort.main()


def test_shadow_arm_overrides_inherited_advisory_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cohort = _cohort()
    monkeypatch.setenv("DIGITALPSYCHOLOGY_SYSTEM_ONE_MODE", "advisory")
    monkeypatch.setenv("DIGITALPSYCHOLOGY_SEMANTIC_PROVIDER", "laya-native")
    monkeypatch.setenv(
        "DIGITALPSYCHOLOGY_SYSTEM_ONE_ACTUATION_EXPERIMENT_ID", "study-1")
    shadow = cohort._agent_env(
        "cfw_csw_dp_laya", state_root=tmp_path / "shadow")
    advisory = cohort._agent_env(
        "cfw_csw_dp_laya_advisory", state_root=tmp_path / "advisory")
    assert shadow["DIGITALPSYCHOLOGY_SYSTEM_ONE_MODE"] == "shadow"
    assert shadow["DIGITALPSYCHOLOGY_SEMANTIC_PROVIDER"] == "laya-native"
    assert "DIGITALPSYCHOLOGY_SYSTEM_ONE_ACTUATION_EXPERIMENT_ID" not in shadow
    assert advisory["DIGITALPSYCHOLOGY_SYSTEM_ONE_MODE"] == "advisory"
    assert advisory["DIGITALPSYCHOLOGY_SEMANTIC_PROVIDER"] == "laya-native"
    assert advisory["DIGITALPSYCHOLOGY_SYSTEM_ONE_ACTUATION_EXPERIMENT_ID"] == "study-1"


def test_laya_evidence_gate_runs_before_preflight_or_model_call(
        monkeypatch: pytest.MonkeyPatch):
    cohort = _cohort()

    def unexpected_preflight(**_kwargs):
        raise AssertionError("preflight must not run before the evidence gate")

    monkeypatch.setattr(cohort.cohort_preflight, "run_canaries", unexpected_preflight)
    monkeypatch.setattr(sys, "argv", [
        "run_cohort.py", "--configurations", "cfw_csw_dp_laya",
        "--trials-per-cell", "1",
    ])
    with pytest.raises(ValueError, match="require --evidence-dir"):
        cohort.main()


def test_evidence_bundle_uses_private_unique_run_directories_and_files(tmp_path: Path):
    cohort = _cohort()
    first = cohort._create_evidence_run(tmp_path / "evidence")
    second = cohort._create_evidence_run(tmp_path / "evidence")
    assert first != second
    assert first.stat().st_mode & 0o777 == 0o700
    output = first / "manifest.json"
    cohort._write_private_json(output, {"status": "running"})
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == {"status": "running"}


def test_evidence_bundle_rejects_symlink_and_shared_writable_parent(tmp_path: Path):
    cohort = _cohort()
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        cohort._create_evidence_run(alias)

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    with pytest.raises(ValueError, match="not writable by others"):
        cohort._create_evidence_run(shared)


def test_cohort_main_persists_manifest_and_cell_artifacts_without_model_calls(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    cohort = _cohort()

    def fake_execute(_contract, _command, *, workspace_root, trial_evidence_out,
                     **_kwargs):
        assert workspace_root is not None
        assert trial_evidence_out is not None
        workspace_root.mkdir(parents=True, mode=0o700)
        trial_evidence_out.write_text("{}\n", encoding="utf-8")
        return {
            "contract_hash": "contract-digest",
            "results": [{
                "task_id": "sample-task",
                "task_family": "code_repair",
                "holdout": False,
                "completion_success": True,
                "latency_seconds": 1.0,
                "evidence_quality": 1.0,
                "regression_rate": 0.0,
                "treatment_exposure": [{
                    "host_generated": True,
                    "configuration": "baseline",
                    "frameworks": [],
                    "context_sha256": "a" * 64,
                    "context_bytes": 12,
                }],
                "host_action_receipts": [{"executed": True}],
                "runtime_behavioral_telemetry": [],
            }],
        }

    monkeypatch.setattr(cohort.benchmark, "execute_benchmark", fake_execute)
    monkeypatch.setattr(sys, "argv", [
        "run_cohort.py", "--configurations", "baseline",
        "--trials-per-cell", "1", "--skip-canaries",
        "--evidence-dir", str(tmp_path / "evidence"),
    ])
    assert cohort.main() == 0
    capsys.readouterr()

    runs = list((tmp_path / "evidence").iterdir())
    assert len(runs) == 1
    run_root = runs[0]
    manifest = json.loads((run_root / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["preflight_status"] == "skipped"
    assert manifest["cells"]["baseline"]["status"] == "complete"
    assert set(manifest["source_revisions"]) == {"CFW", "CSW", "DP"}
    assert manifest["source_revisions"]["CFW"]["head"]
    assert (run_root / "baseline" / "trials.jsonl").is_file()
    assert (run_root / "baseline" / "benchmark-report.json").is_file()
    assert (run_root / "report.json").is_file()
    assert (run_root / "cohort-labels.json").is_file()
    report = json.loads((run_root / "report.json").read_text())
    assert report["paired_effects"] == []
    summary = report["cohorts"]["baseline"]["behavioral_telemetry_summary"]
    assert summary["trials_with_behavioral_record"] == 0
    assert summary["record_snapshots_retained"] == 0


def test_failed_preflight_persists_bounded_failure_before_reraising(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cohort = _cohort()

    def fail_canary(**_kwargs):
        raise cohort.cohort_preflight.PreflightError("private response text")

    def unexpected_benchmark(*_args, **_kwargs):
        raise AssertionError("benchmark must not run after failed preflight")

    monkeypatch.setattr(cohort.cohort_preflight, "run_canaries", fail_canary)
    monkeypatch.setattr(cohort.benchmark, "execute_benchmark", unexpected_benchmark)
    evidence_parent = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", [
        "run_cohort.py", "--configurations", "baseline",
        "--trials-per-cell", "1", "--evidence-dir", str(evidence_parent),
    ])
    with pytest.raises(cohort.cohort_preflight.PreflightError):
        cohort.main()

    run_root = next(evidence_parent.iterdir())
    manifest = json.loads((run_root / "manifest.json").read_text())
    preflight = json.loads((run_root / "preflight.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["preflight_status"] == "failed"
    assert manifest["failure_stage"] == "preflight"
    assert preflight == {"status": "failed", "error_type": "PreflightError"}
    assert "private response text" not in (run_root / "preflight.json").read_text()


def test_baseline_does_not_configure_the_optional_provider(
        monkeypatch: pytest.MonkeyPatch):
    cohort = _cohort()
    monkeypatch.setenv("DIGITALPSYCHOLOGY_SYSTEM_ONE_ACTUATION_EXPERIMENT_ID", "study-1")
    with tempfile.TemporaryDirectory() as raw:
        env = cohort._agent_env("baseline", state_root=Path(raw))
    assert "COGNITIVE_STATEWORK_ROOT" not in env
    assert env["DIGITALPSYCHOLOGY_SEMANTIC_PROVIDER"] == "disabled"
    assert "DIGITALPSYCHOLOGY_SYSTEM_ONE_ACTUATION_EXPERIMENT_ID" not in env


def test_shadow_laya_attestation_does_not_require_calibration_but_advisory_does():
    runtime = _load(
        "cohort_runtime_harness_attestation",
        ROOT / "scripts" / "cohort" / "runtime_harness.py")
    base = {
        "backend_id": "digitalpsychology-laya-native",
        "model_id": "pinned-model",
        "model_revision": "pinned-revision",
        "model_content_hash": "a" * 64,
        "provider_protocol_hash": "b" * 64,
        "calibration_bundle_hash": "",
        "provider_mode": "shadow",
        "capability_digest": "c" * 64,
        "enabled_question_families": ["progress_relation"],
    }

    assert runtime._missing_provider_attestation_fields(base) == []

    advisory_without_calibration = {
        **base,
        "provider_mode": "advisory",
    }
    assert runtime._missing_provider_attestation_fields(
        advisory_without_calibration) == ["calibration_bundle_hash"]

    advisory_with_calibration = {
        **advisory_without_calibration,
        "calibration_bundle_hash": "d" * 64,
    }
    assert runtime._missing_provider_attestation_fields(
        advisory_with_calibration) == []


def test_host_codex_environment_uses_authenticated_cli_without_api_credentials():
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    host = _load("cohort_agent_codex_host_environment",
                 ROOT / "scripts" / "cohort" / "agent_codex_host.py")
    env = host._codex_environment({
        "PATH": "/usr/bin",
        "HOME": "/home/test-user",
        "CODEX_HOME": "/home/test-user/.codex",
        "OPENAI_API_KEY": "api-key-sentinel",
        "OPENROUTER_API_KEY": "router-key-sentinel",
        "HUGGINGFACE_HUB_TOKEN": "hf-token-sentinel",
        "DIGITALPSYCHOLOGY_ROOT": "/tmp/dp",
        "COGNITIVE_STATEWORK_ROOT": "/tmp/csw",
        "PYTHONPATH": "/tmp/project",
        "LANG": "C.UTF-8",
    })
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/test-user",
        "CODEX_HOME": "/home/test-user/.codex",
        "LANG": "C.UTF-8",
    }
    isolated = host._codex_environment(
        {"HOME": "/home/test-user", "PATH": "/usr/bin"},
        writable_home=Path("/tmp/codex-canary-home"),
    )
    assert isolated["HOME"] == "/tmp/codex-canary-home"
    assert isolated["CODEX_HOME"] == "/home/test-user/.codex"


def test_host_codex_diagnostics_bound_protocol_shape_and_redact_secrets():
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    host = _load("cohort_agent_codex_host_diagnostics",
                 ROOT / "scripts" / "cohort" / "agent_codex_host.py")
    stdout = "\n".join([
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "synthetic answer"}}),
        json.dumps({"type": "turn.completed"}),
    ])
    diagnostics = host._codex_jsonl_diagnostics(stdout)
    assert diagnostics["jsonl_event_types"] == [
        "thread.started", "item.completed", "turn.completed"]
    assert diagnostics["jsonl_item_types"] == ["agent_message"]
    assert diagnostics["final_agent_message_present"] is True
    assert "synthetic answer" not in json.dumps(diagnostics)
    excerpt = host._bounded_stderr_excerpt(
        "auth failed Bearer abc123 api_key=secret-value")
    assert "abc123" not in excerpt
    assert "secret-value" not in excerpt


@pytest.mark.parametrize("name", [
    "cfw_csw", "cfw_csw_dp", "cfw_csw_dp_laya",
    "cfw_csw_dp_laya_advisory",
])
def test_workflow_configurations_supply_the_statework_root(name):
    cohort = _cohort()
    with tempfile.TemporaryDirectory() as raw:
        env = cohort._agent_env(name, state_root=Path(raw))
    assert env["COGNITIVE_STATEWORK_ROOT"].endswith("CognitiveStateWork")
    assert env["COGNITIVE_FRAMEWORKS_ROOT"] == str(cohort.ROOT)
    assert env["DIGITALPSYCHOLOGY_ROOT"] == str(cohort.cohort_runtime.DP)


def test_aggregation_separates_holdout_from_treatment():
    cohort = _cohort()
    rows = [
        {"task_family": "code_repair", "holdout": False, "completion_success": True,
         "latency_seconds": 1.0, "evidence_quality": 1.0, "regression_rate": 0.0},
        {"task_family": "code_repair", "holdout": True, "completion_success": False,
         "latency_seconds": 3.0, "evidence_quality": 0.0, "regression_rate": 1.0},
    ]
    per_family = cohort._aggregate(rows)["code_repair"]
    assert per_family["treatment_completion_rate"] == 1.0
    assert per_family["holdout_completion_rate"] == 0.0
    assert per_family["trials"] == 2
    assert per_family["regressions"] == 1


def test_paired_effects_match_trial_ids_and_separate_holdout():
    cohort = _cohort()
    control = [
        {"task_id": "task-1", "task_family": "code_repair", "holdout": False,
         "completion_success": False, "evidence_quality": 0.0,
         "regression_rate": 1.0, "latency_seconds": 10.0,
         "model_calls": 3, "token_overhead": 100, "host_action_count": 4,
         "runtime_behavioral_telemetry": [{
             "semantic_query_opportunities": 2, "semantic_queries": 1,
             "semantic_results": 1, "semantic_uses": 0,
             "semantic_failures": 0, "semantic_influenced_decisions": 0,
             "semantic_deterministic_selection_flips": 0,
         }],
         "metrics": {"hidden_test_pass_rate": 0.0}},
        {"task_id": "task-2", "task_family": "code_repair", "holdout": True,
         "completion_success": True, "evidence_quality": 1.0,
         "regression_rate": 0.0, "latency_seconds": 8.0,
         "model_calls": 2, "token_overhead": 80, "host_action_count": 3,
         "runtime_behavioral_telemetry": [{
             "semantic_query_opportunities": 0, "semantic_queries": 0,
             "semantic_results": 0, "semantic_uses": 0,
             "semantic_failures": 0, "semantic_influenced_decisions": 0,
             "semantic_deterministic_selection_flips": 0,
         }],
         "metrics": {"hidden_test_pass_rate": 1.0}},
    ]
    treatment = [
        {"task_id": "task-1", "task_family": "code_repair", "holdout": False,
         "completion_success": True, "evidence_quality": 1.0,
         "regression_rate": 0.0, "latency_seconds": 12.0,
         "model_calls": 4, "token_overhead": 120, "host_action_count": 3,
         "runtime_behavioral_telemetry": [{
             "semantic_query_opportunities": 2, "semantic_queries": 1,
             "semantic_results": 1, "semantic_uses": 0,
             "semantic_failures": 0, "semantic_influenced_decisions": 0,
             "semantic_deterministic_selection_flips": 0,
         }],
         "metrics": {"hidden_test_pass_rate": 1.0}},
        {"task_id": "task-2", "task_family": "code_repair", "holdout": True,
         "completion_success": True, "evidence_quality": 1.0,
         "regression_rate": 0.0, "latency_seconds": 7.0,
         "model_calls": 1, "token_overhead": 70, "host_action_count": 3,
         "runtime_behavioral_telemetry": [{
             "semantic_query_opportunities": 0, "semantic_queries": 0,
             "semantic_results": 0, "semantic_uses": 0,
             "semantic_failures": 0, "semantic_influenced_decisions": 0,
             "semantic_deterministic_selection_flips": 0,
         }],
         "metrics": {"hidden_test_pass_rate": 1.0}},
        {"task_id": "unmatched", "task_family": "code_repair", "holdout": False,
         "completion_success": True},
    ]
    comparison = cohort._paired_comparison(
        "control", "treatment", {
            "control": {"_raw_results": control},
            "treatment": {"_raw_results": treatment},
        })

    assert comparison["matched_trials"] == 2
    assert comparison["unmatched_treatment_trials"] == 1
    assigned = comparison["by_split"]["assigned"]
    assert assigned["completion_outcomes"]["treatment_only_success"] == 1
    assert assigned["scalar_metrics"]["completion_success"][
        "treatment_minus_control_mean"] == 1.0
    assert assigned["scalar_metrics"]["host_action_count"][
        "treatment_minus_control_mean"] == -1.0
    assert assigned["scalar_metrics"]["model_calls"][
        "treatment_minus_control_mean"] == 1.0
    assert assigned["scalar_metrics"]["semantic_queries"][
        "treatment_minus_control_mean"] == 0.0
    holdout = comparison["by_split"]["holdout"]
    assert holdout["matched_trials"] == 1
    assert holdout["task_evaluator_metrics"]["code_repair"][
        "hidden_test_pass_rate"]["treatment_minus_control_mean"] == 0.0


def test_behavioral_telemetry_uses_final_cumulative_record_once_per_trial():
    cohort = _cohort()
    summary = cohort._behavioral_telemetry_summary([{
        "task_id": "task-1",
        "runtime_behavioral_telemetry": [
            {"semantic_query_opportunities": 1,
             "semantic_queries": 1,
             "semantic_query_planner_skips": 0},
            {"semantic_query_opportunities": 3,
             "semantic_queries": 2,
             "semantic_query_planner_skips": 1,
             "semantic_query_planner_skip_reasons": {"budget_exhausted": 1},
             "semantic_observation_value_estimates": 0,
             "semantic_causal_metrics": {
                 "query_latency_observations": 2,
                 "query_latency_mean_ms": 12.5,
             }},
        ],
    }, {"task_id": "task-2", "runtime_behavioral_telemetry": []}])

    assert summary["trials_with_behavioral_record"] == 1
    assert summary["record_snapshots_retained"] == 2
    assert summary["totals"]["semantic_query_opportunities"] == 3
    assert summary["totals"]["semantic_queries"] == 2
    assert summary["totals"]["semantic_query_planner_skips"] == 1
    assert summary["planner_skip_reasons"] == {"budget_exhausted": 1}
    assert summary["query_latency_observations"] == 2
    assert summary["query_latency_mean_ms"] == 12.5
    assert summary["observation_value_estimates_are_predictions_not_measured_utility"]


def test_report_is_never_marked_qualification_eligible_without_evidence():
    """The harness must not be able to self-certify the cohort gate."""
    cohort = _cohort()
    source = (ROOT / "scripts" / "cohort" / "run_cohort.py").read_text(encoding="utf-8")
    # qualification_eligible is hardcoded False with recorded blockers.
    assert '"qualification_eligible": False' in source
    assert "qualification_blockers" in source
    assert hasattr(cohort, "_aggregate")


def test_agent_adapter_never_fabricates_a_successful_outcome():
    adapter = _load("cohort_adapter_failure", ROOT / "scripts" / "cohort" / "agent_codex.py")
    response = adapter._response_from_agent_message(
        "not JSON", {"input_tokens": 0, "output_tokens": 0})
    answer = json.loads(response["answer"])
    assert response["complete"] is True
    assert answer["status"] == "blocked"
    assert "usable JSON answer" in answer["reason"]
    assert response.get("infrastructure_failure") is not True


def test_agent_adapter_satisfies_the_benchmark_line_protocol():
    """The adapter emits evaluator-compatible JSON and terminal usage."""
    adapter = _load("cohort_adapter_protocol", ROOT / "scripts" / "cohort" / "agent_codex.py")
    answer = {"status": "fixed", "reason": "updated the requested file",
              "files_changed": 1}
    message = json.dumps({"answer": json.dumps(answer), "complete": True})
    events = "\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 31, "output_tokens": 7, "total_tokens": 38}},
    ))
    final_message, usage = adapter.parse_codex_jsonl(events)
    response = adapter._response_from_agent_message(final_message, usage)
    assert json.loads(response["answer"]) == answer
    assert response["complete"] is True
    assert response["usage"] == {"input_tokens": 31, "output_tokens": 7}
    assert response["usage_source"] == "codex.exec.turn.completed"


def test_every_task_family_has_a_field_map_entry():
    """A family with no adapter field map silently scores zero.

    The benchmark contract declares the families; the adapter must name a field
    spec for each so no trial is scored against an unnamed schema.
    """
    cohort = _load("cohort_adapter", ROOT / "scripts" / "cohort" / "agent_codex.py")
    contract = json.loads(
        (ROOT / "contracts" / "real-agent-benchmark-v1.json").read_text(encoding="utf-8"))
    families = [str(f["id"]) for f in contract["task_families"]]
    for family in families:
        spec = cohort._fields_for({"task_family": family})
        assert spec and spec != cohort.COMMON_FIELDS, f"{family} has no field spec"


def test_field_specs_name_the_fields_evaluators_actually_read():
    """Each family spec must name every field its evaluator dereferences.

    The evaluators are the scoring authority. A spec that omits a field the
    evaluator reads means a correct answer parses to {} and scores zero, which
    is exactly how three families reported 0.00 in every cohort. This walks the
    evaluator source and requires every checked field to appear in the adapter's
    spec for that family, so the omission fails here instead of silently.
    """
    adapter_module = _load(
        "cohort_adapter_fields", ROOT / "scripts" / "cohort" / "agent_codex.py")
    tasks_root = ROOT / "scripts" / "real-agent-benchmark-tasks"
    contract = json.loads(
        (ROOT / "contracts" / "real-agent-benchmark-v1.json").read_text(encoding="utf-8"))

    for family in contract["task_families"]:
        family_id = str(family["id"])
        evaluator = _load(f"fields_{family_id}", tasks_root / str(family["task_file"]))
        source = Path(evaluator.__file__).read_text(encoding="utf-8")
        checked = set(re.findall(r"\.get\(\s*['\"]([a-z_]+)['\"]", source))
        # Envelope keys belong to the harness protocol, not the answer payload.
        checked -= {"answer", "response", "trajectory", "task"}
        spec = adapter_module._fields_for({"task_family": family_id})
        if not checked:
            continue
        missing = sorted(field for field in checked if field not in spec)
        assert not missing, (
            f"{family_id} spec omits fields its evaluator reads: {missing}. "
            f"spec={spec!r}")


def test_adapter_parses_multi_line_json_answers():
    """Multi-line final JSON remains intact through Codex JSONL parsing."""
    adapter = _load("cohort_adapter_multiline", ROOT / "scripts" / "cohort" / "agent_codex.py")
    message = '{\n  "answer": "{\\"status\\": \\"fixed\\", \\"files_changed\\": 1}",\n  "complete": true\n}'
    events = "\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 3, "output_tokens": 2}},
    ))
    final_message, usage = adapter.parse_codex_jsonl(events)
    response = adapter._response_from_agent_message(final_message, usage)
    assert json.loads(response["answer"]) == {
        "status": "fixed", "files_changed": 1}


def test_adapter_prefers_the_last_answer_bearing_object():
    """Only the last completed agent message is scored."""
    adapter = _load("cohort_adapter_last_message", ROOT / "scripts" / "cohort" / "agent_codex.py")
    first = json.dumps({"answer": json.dumps({"status": "blocked"}),
                        "complete": True})
    last = json.dumps({"answer": json.dumps({"status": "fixed"}),
                       "complete": True})
    events = "\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": first}},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": last}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 3, "output_tokens": 2}},
    ))
    final_message, usage = adapter.parse_codex_jsonl(events)
    response = adapter._response_from_agent_message(final_message, usage)
    assert json.loads(response["answer"]) == {"status": "fixed"}


def test_adapter_prompt_states_strict_field_types():
    """A counter answered as true/false is a real failure mode seen in runs."""
    source = (ROOT / "scripts" / "cohort" / "agent_codex.py").read_text(encoding="utf-8")
    assert "never true or" in source
    assert "MUST contain every" in source
