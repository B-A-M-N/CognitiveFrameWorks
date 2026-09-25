#!/usr/bin/env python3
"""Measure transfer of a validated learning profile from Agent A to Agent B.

This is a behavioral qualification, not a structural fixture. It requires an
external agent command and compares the same task distribution across control,
shared-adopted, and unrelated holdout cells. A result is not fabricated when
the command is absent; the harness refuses to run and final release status
remains blocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
BENCHMARK = ROOT / "scripts" / "acceptance-real-agent-benchmark.py"
TASKS = ROOT / "scripts" / "real-agent-benchmark-tasks"


def load_benchmark():
    import importlib.util
    spec = importlib.util.spec_from_file_location("real_agent_transfer_benchmark", BENCHMARK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_cell(command: list[str], cell: str, trials: int, *, shared_learning: bool,
             root: Path) -> list[dict[str, Any]]:
    benchmark = load_benchmark()
    results: list[dict[str, Any]] = []
    families = benchmark.load_json(benchmark.CONTRACT_PATH)["task_families"]
    # Use a deterministic unfamiliar-task distribution. The same task family
    # and evaluator version is used in every arm; only adoption differs.
    for trial in range(trials):
        family = families[trial % len(families)]
        task_module = benchmark.load_task_module(TASKS / str(family["task_file"]))
        workspace = root / cell / f"trial-{trial}"
        workspace.mkdir(parents=True)
        task = task_module.create_task(workspace, trial=trial, holdout=cell == "unrelated_holdout")
        task["workspace"] = str(workspace)
        started = time.monotonic()
        try:
            trajectory, usage = benchmark.run_agent(
                command, task, timeout=30, max_model_calls=24,
                max_tokens=250_000, max_cost=10.0, max_elapsed_seconds=30)
            evaluation = task_module.evaluate({"task": task, "trajectory": trajectory}, workspace)
        except Exception as exc:
            evaluation = {"completion_success": False, "evidence_quality": 0.0,
                          "regression_rate": 1.0, "metrics": {},
                          "failure_category": "qualification_error", "error": str(exc)}
            usage = {"model_calls": 0, "external_tokens": 0, "monetary_cost": 0.0}
        results.append({
            "cell": cell, "task_family": task["task_family"], "trial": trial,
            "shared_learning_requested": shared_learning,
            "latency_seconds": time.monotonic() - started,
            "token_overhead": usage["external_tokens"],
            **evaluation,
        })
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for cell in ("control", "shared_adopted", "unrelated_holdout"):
        values = [item for item in results if item["cell"] == cell]
        success = [item["completion_success"] for item in values]
        regressions = [item["regression_rate"] for item in values]
        summary[cell] = {
            "trials": len(values),
            "success_rate": statistics.mean(success) if success else 0.0,
            "regression_rate": statistics.mean(regressions) if regressions else 0.0,
            "latency_p50": statistics.median(item["latency_seconds"] for item in values) if values else 0.0,
            "token_mean": statistics.mean(item["token_overhead"] for item in values) if values else 0.0,
            "evaluator_versions": sorted({str(item.get("evaluator_version")) for item in values}),
            "failures": [item.get("failure_category") for item in values if item.get("failure_category")],
        }
    control = summary["control"]["success_rate"]
    adopted = summary["shared_adopted"]["success_rate"]
    holdout = summary["unrelated_holdout"]["regression_rate"]
    summary["decision"] = {
        "improves_adopted": adopted > control,
        "does_not_regress_holdout": holdout <= 0.0,
        "status": "pass" if adopted > control and holdout <= 0.0 else "fail",
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-command", required=True)
    parser.add_argument("--trials", type=int, default=14)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args()
    if args.trials < 7:
        raise SystemExit("transfer qualification requires at least 7 trials per cell")
    command = shlex.split(args.agent_command)
    if not command:
        raise SystemExit("agent command is empty")
    with tempfile.TemporaryDirectory(prefix="real-agent-transfer-") as raw:
        root = Path(raw)
        results = []
        results.extend(run_cell(command, "control", args.trials, shared_learning=False, root=root))
        results.extend(run_cell(command, "shared_adopted", args.trials, shared_learning=True, root=root))
        results.extend(run_cell(command, "unrelated_holdout", args.trials, shared_learning=True, root=root))
    report = {
        "qualification_id": "real-agent-behavioral-transfer-v1",
        "status": "completed",
        "agent_command_hash": hashlib.sha256(" ".join(command).encode()).hexdigest(),
        "trials_per_cell": args.trials,
        "results": results,
        "summary": summarize(results),
    }
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))
    return 0 if report["summary"]["decision"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
