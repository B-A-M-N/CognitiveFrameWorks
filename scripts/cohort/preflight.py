#!/usr/bin/env python3
"""Benchmark preflight and sanity tripwires.

Every real-agent cohort run must prove the harness itself is sound before any
inference is spent on the matrix. Three successive full runs were discarded
because the adapter silently scored correct work as zero: a nested answer
object, a missing completion flag, a discarded workspace, and field names the
evaluators never read. Each produced a plausible-looking report.

These checks make that class of failure loud. They run before the matrix
(canaries and envelope validation) and after it (hard-zero, full-budget,
missing-provider and missing-telemetry tripwires). A tripwire is an
infrastructure failure, not an experimental outcome, and must abort rather than
be reported as a result.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

TASK_ROOT = Path(__file__).resolve().parent.parent / "real-agent-benchmark-tasks"

# Envelope keys that belong to the harness protocol, not the answer payload.
_ENVELOPE_KEYS = frozenset({"answer", "response", "trajectory", "task", "complete",
                            "usage"})
class PreflightError(RuntimeError):
    """The harness is unsound. Abort; do not report a cohort from this state."""


def _evaluator_fields(source: str) -> set[str]:
    """Field names the evaluator actually dereferences off a parsed answer."""
    fields = set(re.findall(r"\.get\(\s*['\"]([a-z_]+)['\"]", source))
    fields |= set(re.findall(r"\[[\"']([a-z_]+)[\"']\]\.get\(", source))
    return fields - _ENVELOPE_KEYS


def validate_evaluator_contract(family_id: str, task_file: str) -> set[str]:
    """Return the fields an evaluator requires for one task family."""
    path = TASK_ROOT / task_file
    if not path.is_file():
        raise PreflightError(f"task module is missing: {path}")
    return _evaluator_fields(path.read_text(encoding="utf-8"))


def validate_task_envelope(task: Mapping[str, Any]) -> None:
    """Check a task is dispatchable before an agent turn is spent on it."""
    for field in ("task_family", "instruction"):
        if not isinstance(task.get(field), str) or not task[field]:
            raise PreflightError(f"task is missing a usable {field}")
    if len(task["instruction"]) > 4096:
        raise PreflightError("task instruction exceeds its bound")
    workspace = task.get("workspace")
    if workspace is not None:
        if not isinstance(workspace, str) or not Path(workspace).is_dir():
            raise PreflightError("task workspace is not an existing directory")


def validate_agent_response(response: Any, *, required_fields: Iterable[str],
                            workspace: str | None = None) -> None:
    """Check one agent response before it reaches an evaluator."""
    if not isinstance(response, Mapping):
        raise PreflightError("agent response must be a JSON object")
    if response.get("complete") is not True:
        # Without this the benchmark loop burns the full model-call budget and
        # records a resource-limit failure that looks like a model failure.
        raise PreflightError("agent response is missing the completion flag")
    if "answer" not in response:
        raise PreflightError("agent response is missing an answer")
    answer = response["answer"]
    # Evaluators call json.loads() on the answer, so it must be a JSON string.
    if not isinstance(answer, str):
        raise PreflightError("agent answer must be a JSON string, not an object")
    try:
        parsed = json.loads(answer)
    except json.JSONDecodeError as exc:
        raise PreflightError(f"agent answer is not valid JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise PreflightError("agent answer must decode to a JSON object")
    missing = sorted(field for field in required_fields if field not in parsed)
    if missing:
        raise PreflightError(f"agent answer omits evaluated fields: {missing}")
    # Evaluators require a non-empty reason but set no length bound, so this
    # preflight must not invent one and reject correct work.
    if workspace is not None and not Path(workspace).is_dir():
        raise PreflightError("agent removed or lost the task workspace")


def run_canaries(*, agent_command: Sequence[str], families: Sequence[Mapping[str, Any]],
                 canary_count: int = 1) -> list[dict[str, Any]]:
    """Dispatch canary tasks before the matrix and validate every response."""
    import importlib.util
    import subprocess
    import tempfile

    agent_task_view = _load_benchmark().agent_task_view

    adapter = Path(__file__).resolve().parent / "agent_codex.py"
    canaries: list[dict[str, Any]] = []
    for family in families:
        module = _load_task_module(TASK_ROOT / str(family["task_file"]))
        required = validate_evaluator_contract(
            str(family["id"]), str(family["task_file"]))
        for trial in range(canary_count):
            with tempfile.TemporaryDirectory(prefix="cfw-canary-") as raw:
                workspace = Path(raw) / str(family["id"]) / str(trial)
                workspace.mkdir(parents=True)
                task = module.create_task(workspace, trial=trial, holdout=False)
                task["workspace"] = str(workspace)
                validate_task_envelope(task)
                request = json.dumps({"task": agent_task_view(task), "turn": 0,
                                      "previous": []})
                proc = subprocess.run([*agent_command], input=request,
                                      capture_output=True, text=True, timeout=300)
                if proc.returncode != 0 or not proc.stdout.strip():
                    raise PreflightError(
                        f"canary for {family['id']} produced no response")
                response = json.loads(proc.stdout.strip().splitlines()[-1])
                validate_agent_response(response, required_fields=required,
                                        workspace=task["workspace"])
                evaluation = module.evaluate(
                    {"task": task, "trajectory": [{"turn": 0, "response": response}]},
                    workspace)
                canaries.append({
                    "task_family": str(family["id"]),
                    "trial": trial,
                    "required_fields": sorted(required),
                    "completion_success": bool(evaluation["completion_success"]),
                })
    _assert_canaries_scored(canaries)
    return canaries


def _assert_canaries_scored(canaries: Sequence[Mapping[str, Any]]) -> None:
    if not canaries:
        raise PreflightError("no canary trials were dispatched")
    if not any(item["completion_success"] for item in canaries):
        raise PreflightError(
            "every canary scored zero; the harness is unsound, not the agent")


def _load_benchmark():
    """Load the hyphenated benchmark module by path.

    ``acceptance-real-agent-benchmark.py`` cannot be imported by name, so the
    cohort loads it explicitly and this reuses that instance when present.
    """
    module = sys.modules.get("acceptance_real_agent_benchmark")
    if module is not None:
        return module
    path = Path(__file__).resolve().parent.parent / "acceptance-real-agent-benchmark.py"
    spec = importlib.util.spec_from_file_location(
        "acceptance_real_agent_benchmark", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def _load_task_module(path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"preflight_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- post-matrix tripwires ------------------------------------------------

def check_hard_zero(results: Sequence[Mapping[str, Any]]) -> None:
    """A family that is exactly zero in every cell is a harness defect."""
    by_family: dict[str, list[float]] = {}
    for row in results:
        by_family.setdefault(str(row["task_family"]), []).append(
            1.0 if row.get("completion_success") else 0.0)
    for family, values in by_family.items():
        if values and not any(values):
            raise PreflightError(
                f"task family {family!r} scored zero in every configuration; "
                "this is a harness or evaluator defect, not a result")


def check_full_budget_burn(results: Sequence[Mapping[str, Any]],
                           *, threshold: float = 1.0) -> None:
    """Every trial hitting the resource limit means the handshake is broken."""
    if not results:
        return
    burned = sum(1 for row in results
                 if row.get("failure_category") == "resource_limit")
    if burned / len(results) >= threshold:
        raise PreflightError(
            f"{burned}/{len(results)} trials exhausted the action budget; "
            "the completion handshake is broken")


def check_semantic_treatment(manifests: Mapping[str, Mapping[str, Any]]) -> None:
    """A Laya arm with no live provider is an invalid treatment, not a result.

    The treatment must never silently collapse into the DP-only arm.
    """
    required = ("backend_id", "model_id", "model_revision", "model_content_hash",
                "provider_protocol_hash", "calibration_bundle_hash",
                "enabled_question_families")
    for label, manifest in manifests.items():
        if manifest.get("semantic_provider_status") != "enabled":
            raise PreflightError(
                f"cohort {label} claims a semantic treatment but the provider "
                "is not enabled; INVALID_TREATMENT")
        missing = [field for field in required if not manifest.get(field)]
        if missing:
            raise PreflightError(
                f"cohort {label} is missing provider attestation: {missing}")
        if not manifest.get("capability_handshake_observed"):
            raise PreflightError(
                f"cohort {label} recorded no semantic capability handshake")


def check_telemetry_present(reports: Mapping[str, Mapping[str, Any]]) -> None:
    """A gate requiring behavioral telemetry must not run without it."""
    required = ("committed_actions", "max_starvation_age", "fairness_bound",
                "fairness_violation_count", "mode_transitions",
                "rapid_mode_reversals")
    for label, report in reports.items():
        missing = [field for field in required if report.get(field) is None]
        if missing:
            raise PreflightError(
                f"cohort {label} is missing behavioral telemetry: {missing}")


def assert_triads_agree(trial_records: Mapping[str, Any]) -> None:
    """Starvation must be measured from DP's invariant, not inferred."""
    for label, record in trial_records.items():
        if record.get("fairness_violation_count"):
            raise PreflightError(
                f"cohort {label} recorded "
                f"{record['fairness_violation_count']} fairness violations; the "
                "production invariant is zero")


__all__ = ["PreflightError", "validate_evaluator_contract", "validate_task_envelope",
           "validate_agent_response", "run_canaries", "check_hard_zero",
           "check_full_budget_burn", "check_semantic_treatment",
           "check_telemetry_present", "assert_triads_agree"]
