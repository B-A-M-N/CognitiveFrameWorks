#!/usr/bin/env python3
"""Run the separate bounded behavioral benchmark contract.

The agent adapter uses the same one-JSON-request/one-JSON-response protocol as
``acceptance-real-agent.py``. Task-specific evaluators live beside this script;
the benchmark orchestrator never awards success from an agent self-report.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "real-agent-benchmark-v1.json"
TASK_ROOT = Path(__file__).resolve().parent / "real-agent-benchmark-tasks"
AGENT_VISIBLE_TASK_FIELDS = frozenset({"task_id", "task_family", "instruction", "workspace"})


class BenchmarkBudgetExceeded(RuntimeError):
    pass


def _require_private_directory(path: Path) -> None:
    info = path.stat(follow_symlinks=False)
    user_id = getattr(os, "getuid", lambda: info.st_uid)()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != user_id
            or info.st_mode & 0o077):
        raise ValueError("durable benchmark paths must be user-owned and private")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"benchmark JSON must be an object: {path}")
    return value


def load_task_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"real_agent_task_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load task module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "create_task", None)) or not callable(getattr(module, "evaluate", None)):
        raise ValueError(f"task module {path} must define create_task and evaluate")
    return module


def validate_contract(contract: Mapping[str, Any]) -> None:
    required = {"benchmark_id", "evaluator_version", "task_families", "limits", "report_fields"}
    if not required.issubset(contract):
        raise ValueError("behavioral benchmark contract is incomplete")
    families = contract.get("task_families")
    if not isinstance(families, list) or not families:
        raise ValueError("behavioral benchmark requires task families")
    if len({item.get("id") for item in families if isinstance(item, dict)}) != len(families):
        raise ValueError("behavioral benchmark task family IDs must be unique")
    limits = contract.get("limits")
    if not isinstance(limits, dict) or min((float(value) for value in limits.values()), default=0) <= 0:
        raise ValueError("behavioral benchmark limits must be positive")
    for family in families:
        if not isinstance(family, dict) or not family.get("metrics") or not family.get("failure_categories"):
            raise ValueError("each behavioral task family requires metrics and failure categories")
        if not (TASK_ROOT / str(family.get("task_file"))).is_file():
            raise ValueError(f"task module is missing: {family.get('task_file')}")


def agent_task_view(task: Mapping[str, Any]) -> dict[str, Any]:
    """Return task content without evaluation or treatment metadata."""
    return {key: task[key] for key in AGENT_VISIBLE_TASK_FIELDS if key in task}


def run_agent(command: list[str], task: Mapping[str, Any], *, timeout: float,
              max_model_calls: int, max_tokens: int, max_cost: float,
              max_elapsed_seconds: float) -> tuple[list[dict[str, Any]], dict[str, int | float]]:
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
        start_new_session=(os.name == "posix"),
    )
    assert process.stdin is not None and process.stdout is not None
    trajectory: list[dict[str, Any]] = []
    usage = {"model_calls": 0, "external_tokens": 0, "monetary_cost": 0.0}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        for turn in range(max_model_calls + 1):
            if turn >= max_model_calls:
                raise BenchmarkBudgetExceeded("model-call limit reached")
            if time.monotonic() - started > min(timeout, max_elapsed_seconds):
                raise BenchmarkBudgetExceeded("elapsed-time limit reached")
            process.stdin.write(json.dumps({"task": agent_task_view(task),
                                            "turn": turn, "previous": trajectory}) + "\n")
            process.stdin.flush()
            remaining = min(timeout, max_elapsed_seconds) - (time.monotonic() - started)
            if remaining <= 0 or not selector.select(max(0, remaining)):
                raise BenchmarkBudgetExceeded("agent response timeout")
            response_line = process.stdout.readline()
            if not response_line:
                raise RuntimeError("agent closed stdout before trial completion")
            response = json.loads(response_line)
            if not isinstance(response, dict):
                raise ValueError("agent response must be an object")
            trajectory.append({"turn": turn, "response": response})
            usage["model_calls"] += 1
            response_usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            usage["external_tokens"] += int(response_usage.get("input_tokens") or 0) + int(response_usage.get("output_tokens") or 0)
            usage["monetary_cost"] += float(response_usage.get("cost") or 0.0)
            if usage["external_tokens"] > max_tokens:
                raise BenchmarkBudgetExceeded("external-token limit reached")
            if usage["monetary_cost"] > max_cost:
                raise BenchmarkBudgetExceeded("monetary-cost limit reached")
            if response.get("complete") is True:
                return trajectory, usage
        raise BenchmarkBudgetExceeded("model-call limit reached")
    finally:
        selector.close()
        if process.poll() is None:
            if os.name == "posix":
                import signal
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
            else:
                process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    import signal
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                else: process.kill()
                process.wait(timeout=2)
        try: process.stdin.close()
        except OSError: pass
        process.stdout.close()


def execute_benchmark(contract: Mapping[str, Any], command: list[str],
                      *, trials_per_cell: int, holdout_fraction: float,
                      workspace_root: Path | None = None,
                      trial_evidence_out: Path | None = None) -> dict[str, Any]:
    validate_contract(contract)
    limits = dict(contract["limits"])
    results: list[dict[str, Any]] = []
    if trial_evidence_out is not None:
        trial_evidence_out = Path(trial_evidence_out)
        trial_evidence_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_private_directory(trial_evidence_out.parent)
        evidence_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                          | getattr(os, "O_NOFOLLOW", 0))
        evidence_fd = os.open(trial_evidence_out, evidence_flags, 0o600)
        os.fsync(evidence_fd)
        os.close(evidence_fd)
        parent_fd = os.open(
            trial_evidence_out.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    if workspace_root is None:
        root_context = tempfile.TemporaryDirectory(
            prefix="cfw-behavioral-benchmark-")
    else:
        workspace_root = Path(workspace_root)
        workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_private_directory(workspace_root)
        root_context = nullcontext(workspace_root)
    with root_context as raw:
        root = Path(raw)
        for family in contract["task_families"]:
            family_id = str(family["id"])
            task_module = load_task_module(TASK_ROOT / str(family["task_file"]))
            for trial in range(trials_per_cell):
                workspace = root / family_id / str(trial)
                workspace.mkdir(parents=True)
                task = task_module.create_task(workspace, trial=trial, holdout=trial >= int(trials_per_cell * (1 - holdout_fraction)))
                # Stable task identity pairs the same task across treatment
                # cells while keeping every trial's runtime authority scope
                # distinct. It does not expose cohort assignment.
                task["task_id"] = f"{contract['benchmark_id']}-{family_id}-{trial}"
                task["workspace"] = str(workspace)
                started = time.monotonic()
                failure_category = None
                try:
                    trajectory, usage = run_agent(
                        command, task,
                        timeout=float(limits["max_elapsed_seconds_per_trial"]),
                        max_model_calls=int(limits["max_model_calls_per_trial"]),
                        max_tokens=int(limits["max_external_tokens_per_trial"]),
                        max_cost=float(limits["max_monetary_cost_per_trial"]),
                        max_elapsed_seconds=float(limits["max_elapsed_seconds_per_trial"]),
                    )
                    host_receipts = [receipt for turn in trajectory
                                     for receipt in ((turn.get("response") or {}).get(
                                         "host_action_receipts") or ())]
                    treatment_exposure = [dict((turn.get("response") or {}).get(
                        "treatment_exposure") or {}) for turn in trajectory
                        if isinstance((turn.get("response") or {}).get(
                            "treatment_exposure"), Mapping)]
                    runtime_behavioral_telemetry = [dict((turn.get("response") or {}).get(
                        "dp_behavioral_record")) for turn in trajectory
                        if isinstance((turn.get("response") or {}).get(
                            "dp_behavioral_record"), Mapping)]
                    evaluation = task_module.evaluate({
                        "task": task, "trajectory": trajectory,
                        "host_action_receipts": host_receipts,
                        "treatment_exposure": treatment_exposure,
                        "runtime_behavioral_telemetry": runtime_behavioral_telemetry,
                    }, workspace)
                except BenchmarkBudgetExceeded as exc:
                    trajectory, usage = [], {"model_calls": 0, "external_tokens": 0, "monetary_cost": 0.0}
                    evaluation = {"completion_success": False, "evidence_quality": 0.0, "regression_rate": 1.0, "metrics": {}, "failure_category": "resource_limit", "error": str(exc)}
                except Exception as exc:
                    trajectory, usage = [], {"model_calls": 0, "external_tokens": 0, "monetary_cost": 0.0}
                    evaluation = {"completion_success": False, "evidence_quality": 0.0, "regression_rate": 1.0, "metrics": {}, "failure_category": "invalid_completion", "error": type(exc).__name__}
                row = {
                    "task_id": task["task_id"],
                    "task_family": family_id,
                    "trial": trial,
                    "holdout": task["holdout"],
                    "evaluator_version": task["evaluator_version"],
                    "latency_seconds": round(time.monotonic() - started, 3),
                    "model_calls": usage["model_calls"],
                    "token_overhead": usage["external_tokens"],
                    **evaluation,
                }
                # These fields are created by the host adapter and joined by
                # the independent task evaluator. They are not agent answer
                # fields and never contain model self-reported use metadata.
                row["host_action_receipts"] = host_receipts if trajectory else []
                row["host_action_count"] = len(row["host_action_receipts"])
                row["treatment_exposure"] = treatment_exposure if trajectory else []
                row["runtime_behavioral_telemetry"] = (
                    runtime_behavioral_telemetry if trajectory else [])
                # A trial whose agent produced no usable output is an
                # infrastructure failure, not behavioral evidence. Recording it
                # separately stops a flaky backend from being scored as the
                # agent failing the task.
                if (trajectory and isinstance(trajectory[-1], dict)
                        and isinstance(trajectory[-1].get("response"), dict)
                        and trajectory[-1]["response"].get("infrastructure_failure")):
                    row["infrastructure_failure"] = True
                results.append(row)
                if trial_evidence_out is not None:
                    encoded = (json.dumps(
                        row, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                    append_fd = os.open(
                        trial_evidence_out,
                        os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        pending = memoryview(encoded)
                        while pending:
                            written = os.write(append_fd, pending)
                            if written <= 0:
                                raise OSError("short write to trial evidence log")
                            pending = pending[written:]
                        os.fsync(append_fd)
                    finally:
                        os.close(append_fd)
    return {
        "benchmark_id": contract["benchmark_id"],
        "evidence_scope": "task_outcome_only",
        "qualification_eligible": False,
        "qualification_blockers": [
            "no host-assigned system cohort",
            "no verified host-applied intervention receipts",
            "no independent intervention outcome attribution",
        ],
        "contract_hash": hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "trials_per_cell": trials_per_cell,
        "holdout_fraction": holdout_fraction,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-command", required=True)
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    parser.add_argument("--trials-per-cell", type=int, default=3)
    parser.add_argument("--holdout-fraction", type=float, default=0.25)
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()
    if args.trials_per_cell < 1 or not 0 <= args.holdout_fraction < 1:
        raise SystemExit("trials-per-cell must be positive and holdout fraction in [0,1)")
    command = shlex.split(args.agent_command)
    if not command:
        raise SystemExit("agent command is empty")
    report = execute_benchmark(load_json(args.contract), command,
                               trials_per_cell=args.trials_per_cell,
                               holdout_fraction=args.holdout_fraction)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report_out:
        args.report_out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
