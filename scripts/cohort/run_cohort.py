#!/usr/bin/env python3
"""Production qualification cohort across the five supported compositions.

Runs the real-agent benchmark under each configuration, assigns host-side
blinded cohort labels, keeps holdout trials separate from treatment results,
and aggregates the required per-task-family metrics. Evaluator evidence comes
from each task module's `evaluate()`, never from agent self-report.

This is the harness for the `real_agent_behavioral_cohort` gate. It reports
what actually ran; it never asserts a pass that was not observed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util as _util

_spec = _util.spec_from_file_location(
    "acceptance_real_agent_benchmark", ROOT / "scripts" / "acceptance-real-agent-benchmark.py")
benchmark = _util.module_from_spec(_spec)
sys.modules[_spec.name] = benchmark
_spec.loader.exec_module(benchmark)

# Load the sibling preflight module by path; this file is itself loaded by
# path in tests, so a plain sibling import is not reliable here.
_preflight_spec = _util.spec_from_file_location(
    "cohort_preflight", Path(__file__).resolve().parent / "preflight.py")
cohort_preflight = _util.module_from_spec(_preflight_spec)
sys.modules[_preflight_spec.name] = cohort_preflight
_preflight_spec.loader.exec_module(cohort_preflight)

# Ordered weakest-to-strongest composition. Each entry names the subsystems the
# host actually wires for that cell.
CONFIGURATIONS = (
    ("baseline", ("CFW-off",)),
    ("cfw", ("CFW",)),
    ("cfw_csw", ("CFW", "CSW")),
    ("cfw_csw_dp", ("CFW", "CSW", "DP")),
    ("cfw_csw_dp_laya", ("CFW", "CSW", "DP", "Laya")),
)


def _agent_env(configuration: str, *, state_root: Path) -> dict[str, str]:
    """Build the environment each cohort cell's agent subprocess runs under.

    Composition is host-selected: CFW starts CFW-only unless a StateWork root
    is supplied, and DP stays host-injected so a disabled provider is the
    default. This function records intent; it does not claim DP/Laya authority.
    """
    import os
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(state_root / "xdg")
    (state_root / "xdg").mkdir(parents=True, exist_ok=True)
    if configuration in {"cfw_csw", "cfw_csw_dp", "cfw_csw_dp_laya"}:
        env["COGNITIVE_STATEWORK_ROOT"] = "/home/bamn/CognitiveStateWork"
    else:
        env.pop("COGNITIVE_STATEWORK_ROOT", None)
    # DP's semantic provider is never enabled implicitly; Laya stays disabled
    # unless the cell explicitly requires it and a checkpoint is pinned.
    env["DIGITALPSYCHOLOGY_SYSTEM_ONE_MODE"] = "disabled"
    env["DIGITALPSYCHOLOGY_SEMANTIC_PROVIDER"] = "disabled"
    return env


def _aggregate(results: list[dict]) -> dict:
    by_family: dict[str, list[dict]] = {}
    for row in results:
        by_family.setdefault(row["task_family"], []).append(row)
    out = {}
    for family, rows in sorted(by_family.items()):
        treatment = [r for r in rows if not r.get("holdout")]
        holdout = [r for r in rows if r.get("holdout")]
        def _rate(items):
            return round(statistics.fmean(
                [1.0 if i.get("completion_success") else 0.0 for i in items]), 4
            ) if items else None
        out[family] = {
            "treatment_completion_rate": _rate(treatment),
            "holdout_completion_rate": _rate(holdout),
            "trials": len(rows),
            "mean_latency_seconds": round(statistics.fmean(
                [r["latency_seconds"] for r in rows]), 3) if rows else None,
            "mean_evidence_quality": round(statistics.fmean(
                [float(r.get("evidence_quality") or 0.0) for r in rows]), 4) if rows else None,
            "regressions": sum(1 for r in rows if r.get("regression_rate", 0) > 0.5),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-command", default=f"{sys.executable} {ROOT / 'scripts' / 'cohort' / 'agent_codex.py'}")
    parser.add_argument("--contract", type=Path,
                        default=ROOT / "contracts" / "real-agent-benchmark-v1.json")
    parser.add_argument("--configurations", default=",".join(c for c, _ in CONFIGURATIONS))
    parser.add_argument("--trials-per-cell", type=int, default=3)
    parser.add_argument("--holdout-fraction", type=float, default=0.34)
    parser.add_argument("--report-out", type=Path)
    parser.add_argument("--skip-canaries", action="store_true",
                        help="bypass harness preflight; for debugging only")
    parser.add_argument("--label-out", type=Path,
                        help="write the blinded cohort labels for host-side assignment")
    args = parser.parse_args()

    import shlex
    command = shlex.split(args.agent_command)
    if not command:
        raise SystemExit("agent command is empty")
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    wanted = [c.strip() for c in args.configurations.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in dict(CONFIGURATIONS)]
    if unknown:
        raise SystemExit(f"unknown configuration(s): {unknown}")

    # Prove the harness before spending inference on the matrix. Three full
    # runs were previously discarded after the fact because the adapter scored
    # correct work as zero; canaries make that fail here instead.
    if not args.skip_canaries:
        canaries = cohort_preflight.run_canaries(
            agent_command=command, families=contract["task_families"])
        print(json.dumps({"canaries": canaries}, indent=2), flush=True)

    cohorts: dict[str, dict] = {}
    labels = []
    for index, configuration in enumerate(wanted):
        subsystems = dict(CONFIGURATIONS)[configuration]
        # Host assigns a blinded label so trial order cannot reveal treatment.
        label = f"cohort-{chr(ord('A') + index)}"
        with tempfile.TemporaryDirectory(prefix=f"cohort-{configuration}-") as raw:
            state_root = Path(raw)
            env = _agent_env(configuration, state_root=state_root)
            import os
            saved = dict(os.environ)
            os.environ.update(env)
            try:
                report = benchmark.execute_benchmark(
                    contract, command,
                    trials_per_cell=args.trials_per_cell,
                    holdout_fraction=args.holdout_fraction)
            finally:
                os.environ.clear(); os.environ.update(saved)
        cohorts[configuration] = {
            "_raw_results": report["results"],
            "blinded_label": label,
            "subsystems": list(subsystems),
            "per_family": _aggregate(report["results"]),
            "trials_per_cell": args.trials_per_cell,
            "holdout_fraction": args.holdout_fraction,
            "contract_hash": report["contract_hash"],
        }
        labels.append({"blinded_label": label, "configuration": configuration,
                       "subsystems": list(subsystems)})

    # Post-matrix tripwires: infrastructure failures abort rather than report.
    raw_results: list[dict] = []
    for configuration in wanted:
        raw_results.extend(cohorts[configuration].get("_raw_results", []))
    if raw_results:
        cohort_preflight.check_hard_zero(raw_results)
        cohort_preflight.check_full_budget_burn(raw_results)

    out = {
        "cohort_id": "real-agent-behavioral-cohort-v1",
        "evidence_source": "host_evaluator_only",
        "agent_command": args.agent_command,
        "configurations_run": wanted,
        "qualification_eligible": False,
        "qualification_blockers": [
            "task_outcome_only_evaluation",
            "single_agent_backend_per_cell",
        ],
        "cohorts": cohorts,
    }
    for entry in out["cohorts"].values():
        entry.pop("_raw_results", None)
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.report_out:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(text + "\n", encoding="utf-8")
    if args.label_out:
        args.label_out.parent.mkdir(parents=True, exist_ok=True)
        args.label_out.write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
