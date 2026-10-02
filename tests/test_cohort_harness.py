"""Contract tests for the production qualification cohort harness.

These run without a model. They pin the harness guarantees the
`real_agent_behavioral_cohort` gate depends on: blinded host-side cohort
labels, holdout separation, host-evaluator-only evidence, and the five
configurations. They do not assert behavioral outcomes, which only a real
agent run can establish.
"""
from __future__ import annotations

import importlib.util
import json
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


def test_harness_defines_the_five_required_configurations():
    cohort = _cohort()
    names = [name for name, _ in cohort.CONFIGURATIONS]
    assert names == ["baseline", "cfw", "cfw_csw", "cfw_csw_dp", "cfw_csw_dp_laya"]
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


def test_baseline_does_not_configure_the_optional_provider():
    cohort = _cohort()
    with tempfile.TemporaryDirectory() as raw:
        env = cohort._agent_env("baseline", state_root=Path(raw))
    assert "COGNITIVE_STATEWORK_ROOT" not in env
    # DP's provider is never enabled implicitly, in any configuration.
    assert env["DIGITALPSYCHOLOGY_SEMANTIC_PROVIDER"] == "disabled"


@pytest.mark.parametrize("name", ["cfw_csw", "cfw_csw_dp", "cfw_csw_dp_laya"])
def test_workflow_configurations_supply_the_statework_root(name):
    cohort = _cohort()
    with tempfile.TemporaryDirectory() as raw:
        env = cohort._agent_env(name, state_root=Path(raw))
    assert env["COGNITIVE_STATEWORK_ROOT"].endswith("CognitiveStateWork")


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


def test_report_is_never_marked_qualification_eligible_without_evidence():
    """The harness must not be able to self-certify the cohort gate."""
    cohort = _cohort()
    source = (ROOT / "scripts" / "cohort" / "run_cohort.py").read_text(encoding="utf-8")
    # qualification_eligible is hardcoded False with recorded blockers.
    assert '"qualification_eligible": False' in source
    assert "qualification_blockers" in source
    assert hasattr(cohort, "_aggregate")


def test_agent_adapter_never_fabricates_a_successful_outcome():
    source = (ROOT / "scripts" / "cohort" / "agent_codex.py").read_text(encoding="utf-8")
    # An unparsable agent reply must be recorded as blocked, not as success.
    assert '"status": "blocked"' in source
    assert "no parsable JSON from agent" in source


def test_agent_adapter_satisfies_the_benchmark_line_protocol():
    """Pin the two protocol facts the harness depends on.

    The benchmark loop ends a trial only when `complete` is True, and every
    evaluator calls `json.loads(response["answer"])`. Emitting a nested object
    for `answer`, or omitting `complete`, silently scores every correct trial
    as a budget failure.
    """
    import ast
    source = (ROOT / "scripts" / "cohort" / "agent_codex.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    payload_defaults = [
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        and node.target.id == "payload" and isinstance(node.value, ast.Dict)
    ]
    assert payload_defaults, "adapter must declare a default payload"
    default = payload_defaults[0]
    assert default.get("complete") is True, "default payload must end the trial"
    assert default.get("answer") == "", "answer defaults to empty, never a fake success"
    # The answer is serialized to a string, not left as a nested object.
    assert "isinstance(answer, str) else json.dumps(answer)" in source
