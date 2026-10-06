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
