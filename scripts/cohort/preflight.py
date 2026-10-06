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
                 limits: Mapping[str, Any],
                 canary_count: int = 1,
                 max_field_attempts: int = 3) -> list[dict[str, Any]]:
    """Dispatch canary tasks before the matrix and validate every response.

    A real agent occasionally returns an answer missing fields the evaluator
    reads. That is a model omission, not a harness defect, so a canary gets a
    bounded number of retries before the run is aborted. The retry count is
    bounded and reported so a persistently uncooperative agent cannot silently
    be averaged away.
    """
    import importlib.util
    import tempfile

    benchmark = _load_benchmark()

    adapter = Path(__file__).resolve().parent / "agent_codex.py"
    canaries: list[dict[str, Any]] = []
    for family in families:
        module = _load_task_module(TASK_ROOT / str(family["task_file"]))
        required = validate_evaluator_contract(
            str(family["id"]), str(family["task_file"]))
        for trial in range(canary_count):
            response = None
            last_error: Exception | None = None
            for attempt in range(1, max_field_attempts + 1):
                with tempfile.TemporaryDirectory(prefix="cfw-canary-") as raw:
                    workspace = Path(raw) / str(family["id"]) / str(trial)
                    workspace.mkdir(parents=True)
                    task = module.create_task(workspace, trial=trial, holdout=False)
                    task["task_id"] = f"canary-{family['id']}-{trial}"
                    task["workspace"] = str(workspace)
                    validate_task_envelope(task)
                    trajectory, _usage = benchmark.run_agent(
                        list(agent_command), task,
                        timeout=float(limits["max_elapsed_seconds_per_trial"]),
                        max_model_calls=int(limits["max_model_calls_per_trial"]),
                        max_tokens=int(limits["max_external_tokens_per_trial"]),
                        max_cost=float(limits["max_monetary_cost_per_trial"]),
                        max_elapsed_seconds=float(limits[
                            "max_elapsed_seconds_per_trial"]))
                    if not trajectory:
                        last_error = PreflightError(
                            f"canary for {family['id']} produced no response")
                        continue
                    candidate = trajectory[-1].get("response")
                    try:
                        validate_agent_response(
                            candidate, required_fields=required,
                            workspace=task["workspace"])
                    except PreflightError as exc:
                        last_error = exc
                        continue
                    response = candidate
                    evaluation = module.evaluate(
                        {"task": task, "trajectory": trajectory,
                         "host_action_receipts": [
                             receipt for turn in trajectory
                             for receipt in ((turn.get("response") or {}).get(
                                 "host_action_receipts") or ())],
                         "treatment_exposure": [
                             exposure for turn in trajectory
                             for exposure in (((turn.get("response") or {}).get(
                                 "treatment_exposure"),)
                                 if isinstance((turn.get("response") or {}).get(
                                     "treatment_exposure"), Mapping) else ())]},
                        workspace)
                    canaries.append({
                        "task_family": str(family["id"]),
                        "trial": trial,
                        "attempt": attempt,
                        "required_fields": sorted(required),
                        "completion_success": bool(evaluation["completion_success"]),
                    })
                    break
            if response is None:
                raise PreflightError(
                    f"canary for {family['id']} trial {trial} never produced a "
                    f"complete answer after {max_field_attempts} attempts: {last_error}")
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

def _infrastructure_failure(row: Mapping[str, Any]) -> bool:
    """True when a trial failed for harness reasons rather than model behavior."""
    if row.get("infrastructure_failure"):
        return True
    return row.get("failure_category") == "resource_limit"


def check_hard_zero(results: Sequence[Mapping[str, Any]]) -> None:
    """A family that is exactly zero in every configuration is a harness defect.

    Trials that failed for infrastructure reasons are excluded from this check.
    An empty model reply is a flaky backend, and counting it as behavioral
    evidence would let a transient outage masquerade as a result. A family whose
    trials all failed that way is reported separately, because that is also not
    a usable measurement.
    """
    by_family: dict[str, list[Mapping[str, Any]]] = {}
    for row in results:
        by_family.setdefault(str(row["task_family"]), []).append(row)

    all_infrastructure: list[str] = []
    hard_zero: list[str] = []
    for family, rows in by_family.items():
        scored = [row for row in rows if not _infrastructure_failure(row)]
        if not scored:
            all_infrastructure.append(family)
        elif not any(row.get("completion_success") for row in scored):
            hard_zero.append(family)
    if hard_zero:
        raise PreflightError(
            f"task families {sorted(hard_zero)} scored zero in every "
            "configuration; this is a harness or evaluator defect, not a result")
    if all_infrastructure:
        raise PreflightError(
            f"task families {sorted(all_infrastructure)} produced only "
            "infrastructure failures; there is no behavioral evidence for them")


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
                "provider_protocol_hash", "calibration_bundle_hash", "provider_mode",
                "capability_digest", "enabled_question_families")
    digest_fields = ("model_content_hash", "provider_protocol_hash",
                     "calibration_bundle_hash", "capability_digest")
    for label, manifest in manifests.items():
        if manifest.get("semantic_provider_status") != "enabled":
            raise PreflightError(
                f"cohort {label} claims a semantic treatment but the provider "
                "is not enabled; INVALID_TREATMENT")
        missing = [field for field in required if not manifest.get(field)]
        if missing:
            raise PreflightError(
                f"cohort {label} is missing provider attestation: {missing}")
        if manifest.get("capability_handshake_observed") is not True:
            raise PreflightError(
                f"cohort {label} recorded no semantic capability handshake")
        if manifest.get("backend_id") != "digitalpsychology-laya-native":
            raise PreflightError(
                f"cohort {label} attested a provider other than native Laya")
        if manifest.get("provider_mode") not in {"shadow", "advisory"}:
            raise PreflightError(
                f"cohort {label} has an invalid semantic provider mode")
        for field in digest_fields:
            if not isinstance(manifest.get(field), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", manifest[field]):
                raise PreflightError(
                    f"cohort {label} has an invalid provider attestation digest: {field}")
        families = manifest.get("enabled_question_families")
        if (not isinstance(families, list) or not families or len(families) > 16
                or any(not isinstance(item, str) or not item for item in families)
                or len(families) != len(set(families))):
            raise PreflightError(
                f"cohort {label} has invalid enabled question families")


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
