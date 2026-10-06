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
from typing import Any

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

_harness_spec = _util.spec_from_file_location(
    "cohort_runtime_harness", Path(__file__).resolve().parent / "runtime_harness.py")
cohort_runtime = _util.module_from_spec(_harness_spec)
sys.modules[_harness_spec.name] = cohort_runtime
_harness_spec.loader.exec_module(cohort_runtime)

# Ordered weakest-to-strongest composition. Each entry names the subsystems the
# host actually wires for that cell.
CONFIGURATIONS = (
    ("baseline", ("CFW-off",)),
    ("cfw", ("CFW",)),
    ("cfw_csw", ("CFW", "CSW")),
    ("cfw_csw_dp", ("CFW", "CSW", "DP")),
    ("cfw_csw_dp_laya", ("CFW", "CSW", "DP", "Laya")),
)
EXPECTED_FRAMEWORKS = {
    "baseline": (),
    "cfw": ("CFW",),
    "cfw_csw": ("CFW", "CSW"),
    "cfw_csw_dp": ("CFW", "CSW", "DP"),
    "cfw_csw_dp_laya": ("CFW", "CSW", "DP", "Laya"),
}


def _agent_env(configuration: str, *, state_root: Path) -> dict[str, str]:
    """Build the environment for one cell's trusted host and private DP child."""
    import os
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(state_root / "xdg")
    env["COGNITIVE_FRAMEWORKS_ROOT"] = str(ROOT)
    env["DIGITALPSYCHOLOGY_ROOT"] = str(cohort_runtime.DP)
    (state_root / "xdg").mkdir(parents=True, exist_ok=True)
    if configuration in {"cfw_csw", "cfw_csw_dp", "cfw_csw_dp_laya"}:
        env["COGNITIVE_STATEWORK_ROOT"] = str(cohort_runtime.COW)
    else:
        env.pop("COGNITIVE_STATEWORK_ROOT", None)
    # Ordinary cells always run without System-1. A Laya cell inherits only
    # explicit provider settings; the private MCP server must attest them.
    if configuration != "cfw_csw_dp_laya":
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
    parser.add_argument("--agent-command", default=(
        f"{sys.executable} {ROOT / 'scripts' / 'cohort' / 'agent_codex_host.py'}"))
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
            agent_command=command, families=contract["task_families"],
            limits=contract["limits"])
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
            cell_command = [*command, "--configuration", configuration,
                            "--state-root", str(state_root / "host-state")]
            os.environ.update(env)
            try:
                report = benchmark.execute_benchmark(
                    contract, cell_command,
                    trials_per_cell=args.trials_per_cell,
                    holdout_fraction=args.holdout_fraction)
            finally:
                os.environ.clear(); os.environ.update(saved)

        raw_results = report["results"]
        exposures = [exposure for row in raw_results
                     for exposure in row.get("treatment_exposure", [])]
        receipts = [receipt for row in raw_results
                    for receipt in row.get("host_action_receipts", [])]
        expected_frameworks = list(EXPECTED_FRAMEWORKS[configuration])
        exposure_verified = bool(raw_results) and all(
            bool(exposure.get("host_generated"))
            and exposure.get("configuration") == configuration
            and exposure.get("frameworks") == expected_frameworks
            and bool(exposure.get("context_sha256"))
            and int(exposure.get("context_bytes", 0)) > 0
            and ("DP" not in expected_frameworks
                 or bool(exposure.get("private_dp_mcp")))
            and ("CSW" not in expected_frameworks
                 or (bool(exposure.get("csw_authority_id"))
                     and bool(exposure.get("csw_registry_hash"))))
            for exposure in exposures) and all(
                bool(row.get("treatment_exposure")) for row in raw_results)
        def _authoritative_receipt(item: dict) -> bool:
            if item.get("executed") is not True:
                return False
            if "CFW" not in expected_frameworks:
                return True
            cfw_gate = item.get("cfw_gate") or {}
            if "DP" not in expected_frameworks:
                return cfw_gate.get("decision") == "allow"
            boundary = item.get("dp_committed_boundary") or {}
            gate_receipts = item.get("cfw_gate_receipts") or []
            return (cfw_gate.get("status") == "ready"
                    and cfw_gate.get("action_authorized") is True
                    and boundary.get("status") == "ready"
                    and any(receipt.get("stage") == "final"
                            and receipt.get("decision") == "allow"
                            for receipt in gate_receipts))

        receipt_verified = bool(raw_results) and all(
            any(_authoritative_receipt(item) for item in row.get(
                "host_action_receipts", [])) for row in raw_results)
        if configuration == "cfw_csw_dp_laya":
            for row in raw_results:
                per_trial = row.get("runtime_behavioral_telemetry") or []
                if not per_trial:
                    exposure_verified = False
                    continue
                for record in per_trial:
                    cohort_preflight.check_semantic_treatment({
                        str(record.get("trial_id") or row.get("task_family")): record})

        cell_blockers = []
        if not exposure_verified:
            cell_blockers.append("treatment_exposure_missing_or_mismatched")
        if not receipt_verified:
            cell_blockers.append("host_action_receipts_missing")

        cohorts[configuration] = {
            "_raw_results": raw_results,
            "runtime_behavioral_telemetry": [
                telemetry for row in raw_results
                for telemetry in row.get("runtime_behavioral_telemetry", [])],
            "treatment_exposure_verified": exposure_verified,
            "host_action_receipts_verified": receipt_verified,
            "validation_blockers": cell_blockers,
            "host_action_count": len(receipts),
            "treatment_exposure_count": len(exposures),
            "blinded_label": label,
            "subsystems": list(subsystems),
            "per_family": _aggregate(raw_results),
            "trials_per_cell": args.trials_per_cell,
            "holdout_fraction": args.holdout_fraction,
            "contract_hash": report["contract_hash"],
        }
        labels.append({"blinded_label": label, "configuration": configuration,
                       "subsystems": list(subsystems)})

    all_exposure_verified = all(
        cohorts[name]["treatment_exposure_verified"] for name in wanted)
    all_receipts_verified = all(
        cohorts[name]["host_action_receipts_verified"] for name in wanted)

    # Post-matrix tripwires: infrastructure failures abort rather than report.
    raw_results: list[dict] = []
    for configuration in wanted:
        raw_results.extend(cohorts[configuration].get("_raw_results", []))
    if raw_results:
        cohort_preflight.check_hard_zero(raw_results)
        cohort_preflight.check_full_budget_burn(raw_results)

    qualification_blockers = [
        "full_agent_tool_registry_not_observable",
        "independent_intervention_outcome_attribution_incomplete",
        "cross_cell_real_agent_effect_not_established",
    ]
    if not all_exposure_verified:
        qualification_blockers.append("treatment_exposure_incomplete")
    if not all_receipts_verified:
        qualification_blockers.append("host_action_receipts_incomplete")

    out = {
        "cohort_id": "real-agent-behavioral-cohort-v1",
        "evidence_source": "host_evaluator_with_host_action_receipts",
        "agent_command": args.agent_command,
        "configurations_run": wanted,
        "treatment_exposure_verified": all_exposure_verified,
        "host_action_receipts_verified": all_receipts_verified,
        "qualification_eligible": False,
        "qualification_blockers": qualification_blockers,
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
