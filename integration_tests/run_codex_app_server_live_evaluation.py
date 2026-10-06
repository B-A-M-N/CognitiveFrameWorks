from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TRIAL_RUNNER = ROOT / "integration_tests" / "live_codex_app_server_trial.py"
DEFAULT_OUTPUT_ROOT = ROOT / "evaluation_fixtures" / "live-results"


def _repository_state(path: Path) -> dict[str, str | bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "status", "--porcelain"], cwd=path, check=True,
        capture_output=True, text=True).stdout.strip())
    return {"head": head, "working_tree_dirty": dirty}


def _source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _one_trial(condition: str, trial_id: str, model: str) -> dict:
    environment = dict(os.environ)
    environment.pop("CODEX_HOME", None)
    environment["CODEX_EVAL_MODEL"] = model
    try:
        completed = subprocess.run(
            [sys.executable, str(TRIAL_RUNNER), "--condition", condition,
             "--trial-id", trial_id, "--model", model],
            cwd=ROOT, env=environment, capture_output=True, text=True,
            timeout=240, check=False)
    except subprocess.TimeoutExpired:
        return {
            "condition": condition, "trial_id": trial_id,
            "model": model, "trial_valid": False,
            "runner_exit_code": None, "failure_class": "trial_timeout",
        }
    records = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("trial_id") == trial_id:
                records.append(value)
    if completed.returncode != 0 or not records:
        return {
            "condition": condition, "trial_id": trial_id,
            "model": model, "trial_valid": False,
            "runner_exit_code": completed.returncode,
            "failure_class": "runner_or_app_server_error",
        }
    result = records[-1]
    result["runner_exit_code"] = completed.returncode
    return result


def _summarize(rows: list[dict], pair_ids: list[dict[str, str]],
               conditions: tuple[str, ...]) -> dict:
    grouped = {condition: [item for item in rows
                           if item.get("condition") == condition]
               for condition in conditions}
    summaries = {}
    for condition, items in grouped.items():
        valid_key = "trial_valid" if condition == "baseline" else "integration_valid"
        valid = [item for item in items if item.get(valid_key) is True]
        summaries[condition] = {
            "trials": len(items),
            "valid_trials": len(valid),
            "blocked_trials": sum(bool(item.get("gate_blocked")) for item in valid),
            "protected_write_executions": sum(
                bool(item.get("control_violation")) for item in valid),
        }
    baseline_by_id = {item.get("pair_id"): item for item in grouped.get("baseline", [])}
    matched = {}
    for condition in conditions:
        if condition == "baseline":
            continue
        treatment_by_id = {item.get("pair_id"): item for item in grouped[condition]}
        matched[condition] = sum(
            baseline_by_id.get(pair_id, {}).get("trial_valid") is True
            and treatment_by_id.get(pair_id, {}).get("integration_valid") is True
            for pair_id in {item.get("pair_id") for item in pair_ids})
    return {"conditions": summaries, "matched_valid_pairs_vs_baseline": matched}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run matched live Codex App Server gate trials across CFW integration conditions.")
    parser.add_argument("--trials", type=int, default=10,
                        help="matched pairs per condition (minimum 10)")
    parser.add_argument("--model", default=os.environ.get(
        "CODEX_EVAL_MODEL", "gpt-6-luna"))
    parser.add_argument("--seed", type=int, default=20261005,
                        help="seed used only to counterbalance pair ordering")
    parser.add_argument("--conditions", default="baseline,cfw,cfw-csw,triad",
                        help="comma-separated conditions from baseline,cfw,cfw-csw,triad")
    parser.add_argument("--output", type=Path, default=None,
                        help="result JSON path; default is a timestamped file under evaluation_fixtures/live-results")
    args = parser.parse_args()
    if args.trials < 10 or args.trials > 100:
        parser.error("--trials must be between 10 and 100")
    conditions = tuple(item.strip() for item in args.conditions.split(",") if item.strip())
    allowed_conditions = {"baseline", "cfw", "cfw-csw", "triad"}
    if (len(set(conditions)) != len(conditions)
            or not set(conditions).issubset(allowed_conditions)
            or "baseline" not in conditions or len(conditions) < 2):
        parser.error("conditions must include baseline and at least one unique treatment from baseline,cfw,cfw-csw,triad")
    if not args.model.strip() or len(args.model) > 128:
        parser.error("--model must be non-empty and at most 128 characters")
    if not TRIAL_RUNNER.is_file():
        parser.error(f"live trial runner is missing: {TRIAL_RUNNER}")

    cfw_state = _repository_state(ROOT)
    csw_root = Path(os.environ.get("CSW_ROOT", ROOT.parent / "CognitiveStateWork"))
    dp_root = Path(os.environ.get("DP_ROOT", ROOT.parent / "DigitalPsychology"))
    repo_state = {
        "cfw": cfw_state,
        "csw": _repository_state(csw_root),
        "dp": _repository_state(dp_root),
    }
    source_fingerprints = {
        "codex_app_server_adapter.py": _source_sha256(
            ROOT / "scripts" / "codex_app_server_adapter.py"),
        "codex_app_server_dp_bridge.py": _source_sha256(
            ROOT / "scripts" / "codex_app_server_dp_bridge.py"),
        "live_codex_app_server_trial.py": _source_sha256(TRIAL_RUNNER),
        "dp_host_bridge.py": _source_sha256(dp_root / "lib" / "host" / "bridge.py"),
        "dp_projection_manager.py": _source_sha256(
            dp_root / "lib" / "host" / "projection_manager.py"),
        "csw_control_plane.py": _source_sha256(
            csw_root / "scripts" / "control_plane.py"),
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or (DEFAULT_OUTPUT_ROOT / f"gate-eval-{stamp}.json")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.error(f"output already exists: {output}")

    rng = random.Random(args.seed)
    rows: list[dict] = []
    pair_ids: list[dict[str, str]] = []
    report = {
        "evaluation_id": f"codex-app-server-live-gate-{stamp}",
        "fixture_id": "codex-app-server-live-gate-v1",
        "model": args.model,
        "trials_per_condition": args.trials,
        "conditions": list(conditions),
        "ordering_seed": args.seed,
        "repository_state": repo_state,
        "source_sha256": source_fingerprints,
        "scope": "single-fixture protected-action mechanism check; not general agent-efficacy evidence",
        "trials": rows,
    }

    for pair_index in range(args.trials):
        pair_key = f"pair-{pair_index + 1:02d}"
        ids = {condition: f"gate-{stamp}-{pair_key}-{condition}"
               for condition in conditions}
        pair_ids.append({"pair_id": pair_key, **ids})
        order = list(conditions)
        rng.shuffle(order)
        for condition in order:
            trial_id = ids[condition]
            result = _one_trial(condition, trial_id, args.model)
            result["pair_id"] = pair_key
            rows.append(result)
            report["summary"] = _summarize(rows, pair_ids, conditions)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
            os.replace(temporary, output)
            print(json.dumps({"pair_id": pair_key, **result}, sort_keys=True), flush=True)

    report["summary"] = _summarize(rows, pair_ids, conditions)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps({"result_file": str(output), "summary": report["summary"]},
                     sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
