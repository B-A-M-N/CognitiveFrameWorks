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
    """The model often emits its JSON across several lines.

    Scanning line-at-a-time missed those entirely and the adapter reported "no
    parsable JSON" for a correct answer, which scored the trial as a failure.
    """
    source = (ROOT / "scripts" / "cohort" / "agent_codex.py").read_text(encoding="utf-8")
    assert "raw.splitlines()" not in source.split("parsed_objects")[0][-400:], (
        "the parser must not scan the output one line at a time")
    assert 'lines[start:end]' in source, "must scan multi-line chunks"
    assert "no parsable JSON from agent" in source


def test_adapter_prefers_the_last_answer_bearing_object():
    """Codex prints banner and metadata lines before the model's answer."""
    source = (ROOT / "scripts" / "cohort" / "agent_codex.py").read_text(encoding="utf-8")
    assert "for parsed in reversed(parsed_objects)" in source, (
        "must prefer the last JSON object that carries an answer")


def test_adapter_prompt_states_strict_field_types():
    """A counter answered as true/false is a real failure mode seen in runs."""
    source = (ROOT / "scripts" / "cohort" / "agent_codex.py").read_text(encoding="utf-8")
    assert "never true or" in source
    assert "MUST contain every" in source
