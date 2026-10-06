from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TRIAL_RUNNER = ROOT / "integration_tests" / "live_codex_app_server_coding_trial.py"
FIXTURE = ROOT / "evaluation_fixtures" / "codex-app-server-coding-v1"
DEFAULT_OUTPUT_ROOT = ROOT / "evaluation_fixtures" / "live-results"
CONDITIONS = ("baseline", "cfw", "cfw-csw", "triad", "triad-multipath")
CRITERIA = ("parser_correctness", "retry_schedule", "state_removal",
            "event_integration")

from codex_eval.behavior_scenarios import SCENARIOS
from codex_eval.trace_analysis import compare_behavior_trace_outcomes


def _repository_state(path: Path) -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "-z"],
        cwd=path, check=True, capture_output=True).stdout
    diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=path,
                          check=True, capture_output=True).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=path, check=True, capture_output=True).stdout
    file_hashes = {}
    skipped_sensitive = 0
    for raw_name in untracked.split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", errors="replace")
        lowered = Path(name).name.lower()
        if (lowered.startswith(".env") or any(token in lowered for token in
                ("credential", "secret", "private-key", "api-key"))):
            skipped_sensitive += 1
            continue
        candidate = path / name
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            file_hashes[name] = _sha256(candidate)
        except OSError:
            continue
    untracked_manifest = json.dumps(file_hashes, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")
    return {
        "head": head,
        "working_tree_dirty": bool(status),
        "status_entry_count": len([item for item in status.split(b"\0") if item]),
        "working_tree_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "untracked_file_hashes": file_hashes,
        "untracked_manifest_sha256": hashlib.sha256(untracked_manifest).hexdigest(),
        "sensitive_untracked_files_omitted": skipped_sensitive,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_fingerprint() -> dict[str, str]:
    return {path.relative_to(FIXTURE).as_posix(): _sha256(path)
            for path in sorted(FIXTURE.rglob("*")) if path.is_file()}


def _codex_host_source_manifest() -> dict[str, object]:
    roots = (
        ROOT / "scripts" / "codex_host",
        ROOT / "integration_tests" / "codex_eval",
    )
    explicit = (
        ROOT / "scripts" / "codex_app_server_adapter.py",
        ROOT / "scripts" / "codex_app_server_dp_bridge.py",
        ROOT / "integration_tests" / "live_codex_app_server_coding_trial.py",
        ROOT / "integration_tests" / "run_codex_app_server_coding_evaluation.py",
    )
    files = set(explicit)
    for source_root in roots:
        if source_root.is_dir():
            files.update(path for path in source_root.rglob("*")
                         if path.is_file() and "__pycache__" not in path.parts)
    manifest = {path.relative_to(ROOT).as_posix(): _sha256(path)
                for path in sorted(files) if path.is_file()}
    canonical = json.dumps(manifest, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(canonical).hexdigest(), "files": manifest}


def _one_trial(condition: str, trial_id: str, model: str,
               max_turns: int, *, auth_mode: str,
               provider_id: str | None, provider_base_url: str | None,
               provider_env_key: str | None, provider_wire_api: str,
               provider_env_file: Path | None, codex_binary: str | None,
               behavioral_scenario: str | None) -> dict:
    environment = dict(os.environ)
    environment.pop("CODEX_HOME", None)
    environment["CODEX_EVAL_MODEL"] = model
    command = [sys.executable, str(TRIAL_RUNNER), "--condition", condition,
               "--trial-id", trial_id, "--model", model,
               "--max-turns", str(max_turns), "--auth-mode", auth_mode,
               "--provider-wire-api", provider_wire_api,
               "--sanitized-json"]
    if provider_env_file is not None:
        command.extend(("--provider-env-file", str(provider_env_file)))
    if provider_id:
        command.extend(("--provider-id", provider_id))
    if provider_base_url:
        command.extend(("--provider-base-url", provider_base_url))
    if provider_env_key:
        command.extend(("--provider-env-key", provider_env_key))
    if behavioral_scenario:
        command.extend(("--behavioral-scenario", behavioral_scenario))
    if codex_binary:
        environment["CODEX_APP_SERVER_BIN"] = codex_binary
    try:
        completed = subprocess.run(command, cwd=ROOT, env=environment,
                                   capture_output=True, text=True,
                                   timeout=360, check=False)
    except subprocess.TimeoutExpired:
        return {"condition": condition, "trial_id": trial_id, "model": model,
                "runner_exit_code": None, "trial_valid": False,
                "inference_ready": False, "model_execution_observed": False,
                "infrastructure_valid": False, "model_infrastructure_valid": False,
                "efficacy_scored": False, "efficacy_valid": False,
                "host_integration_valid": False,
                "failure_domain": "codex_app_server_inference",
                "failure_class": "trial_timeout"}
    records = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("trial_id") == trial_id:
            records.append(value)
    if completed.returncode != 0 or not records:
        failure = dict(records[-1]) if records else {}
        stderr_tail = completed.stderr[-1200:]
        stderr_tail = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", stderr_tail)
        stderr_tail = re.sub(
            r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+",
            r"\1=[redacted]", stderr_tail)
        return {
            **failure, "condition": condition, "trial_id": trial_id,
            "model": model, "runner_exit_code": completed.returncode,
            "trial_valid": False,
            "inference_ready": failure.get("inference_ready") is True,
            "model_execution_observed": failure.get(
                "model_execution_observed") is True,
            "infrastructure_valid": failure.get("infrastructure_valid") is True,
            "model_infrastructure_valid": failure.get(
                "model_infrastructure_valid", False) is True,
            "efficacy_scored": False, "efficacy_valid": False,
            "host_integration_valid": failure.get("host_integration_valid") is True,
            "failure_domain": failure.get("failure_domain")
                or "codex_app_server_inference",
            "failure_class": failure.get("failure_class")
                or "runner_or_app_server_error",
            "upstream_blocked": failure.get("failure_domain")
                == "codex_app_server_workspace_routing",
            "failure_message": failure.get("failure_message"),
            "runner_stderr_tail": stderr_tail,
        }
    result = records[-1]
    result["runner_exit_code"] = completed.returncode
    result.setdefault("host_integration_valid",
                      (condition == "baseline" and
                       result.get("inference_ready") is True))
    result["efficacy_valid"] = bool(
        result.get("model_infrastructure_valid",
                   result.get("infrastructure_valid")) is True
        and result.get("inference_ready") is True
        and result.get("model_execution_observed") is True
        and result.get("efficacy_scored") is True)
    result["upstream_blocked"] = result.get("failure_class") in {
        "workspace_routing_unavailable", "workspace_routing_discovery_failed"} \
        or result.get("failure_domain") == "codex_app_server_workspace_routing"
    result["trial_valid"] = (
        result.get("inference_ready") is True
        and result.get("model_execution_observed") is True
        and result.get("turn_status") == "completed"
        and (condition == "baseline" or result.get("integration_valid") is True))
    return result


def _mean(values: list[float | int]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


def _median(values: list[float | int]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _distribution(values: list[float | int]) -> dict[str, int | float | None]:
    selected = [float(value) for value in values
                if type(value) in {int, float} and math.isfinite(float(value))]
    if not selected:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(selected),
        "mean": round(statistics.fmean(selected), 3),
        "median": round(statistics.median(selected), 3),
        "min": round(min(selected), 3),
        "max": round(max(selected), 3),
    }


def _trace_outcome_summary(trials: list[dict]) -> dict[str, dict]:
    cohorts: dict[str, list[dict]] = {"complete_4_of_4": [], "below_4_of_4": []}
    for row in trials:
        score = row.get("criteria_passed")
        if type(score) is int:
            cohorts["complete_4_of_4" if score == len(CRITERIA)
                    else "below_4_of_4"].append(row)
    result = {}
    for name, cohort in cohorts.items():
        mode_counts: dict[str, int] = {}
        anchor_counts: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        guidance_bytes = []
        turn_counts = []
        reanchor_turns = 0
        trace_turns = 0
        for row in cohort:
            trace = row.get("behavior_trace") or []
            if not isinstance(trace, list):
                continue
            turn_counts.append(len(trace))
            for item in trace:
                if not isinstance(item, dict):
                    continue
                trace_turns += 1
                size = item.get("guidance_byte_count")
                if type(size) is int:
                    guidance_bytes.append(size)
                mode = item.get("behavioral_mode")
                if isinstance(mode, str):
                    mode_counts[mode] = mode_counts.get(mode, 0) + 1
                anchor = item.get("goal_anchor_state")
                if isinstance(anchor, str):
                    anchor_counts[anchor] = anchor_counts.get(anchor, 0) + 1
                for reason in item.get("reason_codes", ()):
                    if isinstance(reason, str):
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                reanchor_turns += item.get("goal_reanchor_required") is True
        result[name] = {
            "trials": len(cohort), "trace_turns": trace_turns,
            "turn_count_distribution": _distribution(turn_counts),
            "guidance_byte_distribution": _distribution(guidance_bytes),
            "behavioral_mode_counts": mode_counts,
            "goal_anchor_state_counts": anchor_counts,
            "reason_code_counts": reason_counts,
            "goal_reanchor_turns": reanchor_turns,
        }
    result["descriptive_comparison"] = compare_behavior_trace_outcomes(trials)
    return result


def _summarize(rows: list[dict], pairs: list[dict[str, str]],
               conditions: tuple[str, ...]) -> dict:
    grouped = {condition: [row for row in rows
                           if row.get("condition") == condition]
               for condition in conditions}
    per_condition = {}
    for condition, trials in grouped.items():
        valid = [row for row in trials if row.get("efficacy_valid") is True]
        completion_observed = [row for row in valid
                               if type(row.get("completion_permitted")) is bool]
        bridge_statuses = [row.get("cognitive_host_status") or {}
                           for row in trials
                           if condition in {"triad", "triad-multipath"}]
        dp_intervention_metrics = [
            status.get("dp_intervention_metrics") or {}
            for status in bridge_statuses]
        dp_metrics = [
            ((row.get("cognitive_host_status") or {})
             .get("production_metrics") or {})
            for row in trials if condition in {"triad", "triad-multipath"}
        ]
        cfw_metrics = []
        for metric in dp_metrics:
            if not isinstance(metric, Mapping):
                cfw_metrics.append({})
                continue
            # `production_counters` was emitted by an older sanitized bridge.
            # Prefer canonical counters per key while retaining legacy-only keys.
            legacy = metric.get("production_counters")
            canonical = metric.get("counters")
            counters = dict(legacy) if isinstance(legacy, Mapping) else {}
            if isinstance(canonical, Mapping):
                counters.update(canonical)
            cfw_metrics.append(counters)
        validator_channels: dict[str, dict[str, Any]] = {}
        for row in valid:
            raw_channels = row.get("validator_channel_metrics") or {}
            if not isinstance(raw_channels, dict):
                continue
            for channel, metrics in raw_channels.items():
                if not isinstance(metrics, dict):
                    continue
                aggregate = validator_channels.setdefault(channel, {
                    "runs": 0, "passed": 0, "failed": 0,
                    "total_elapsed_ms": 0.0, "trial_elapsed_ms": [],
                })
                for key in ("runs", "passed", "failed"):
                    if type(metrics.get(key)) is int and metrics[key] >= 0:
                        aggregate[key] += metrics[key]
                total = metrics.get("total_elapsed_ms")
                if (type(total) in {int, float} and math.isfinite(float(total))
                        and total >= 0):
                    aggregate["total_elapsed_ms"] += total
                    aggregate["trial_elapsed_ms"].append(total)
        validator_channels = {
            channel: {
                "runs": metrics["runs"], "passed": metrics["passed"],
                "failed": metrics["failed"],
                "total_elapsed_ms": round(metrics["total_elapsed_ms"], 3),
                "trial_elapsed_ms_distribution": _distribution(
                    metrics["trial_elapsed_ms"]),
            }
            for channel, metrics in sorted(validator_channels.items())}
        per_condition[condition] = {
            "trials": len(trials),
            "scheduled_trials": len(trials),
            "host_valid_trials": sum(row.get("host_integration_valid") is True
                                      for row in trials),
            "inference_ready_trials": sum(row.get("inference_ready") is True
                                           for row in trials),
            "model_started_trials": sum(row.get("model_execution_observed") is True
                                         for row in trials),
            "efficacy_valid_trials": len(valid),
            "upstream_blocked_trials": sum(row.get("upstream_blocked") is True
                                            for row in trials),
            "valid_trials": len(valid),
            "complete_fixture_trials": sum(
                row.get("criteria_passed") == len(CRITERIA) for row in valid),
            "full_fixture_score_rate": (
                sum(row.get("criteria_passed") == len(CRITERIA) for row in valid)
                / len(valid) if valid else None),
            "mean_criteria_passed": _mean([
                row.get("criteria_passed", 0) for row in valid]),
            "criteria_passed_distribution": _distribution([
                row.get("criteria_passed") for row in valid
                if type(row.get("criteria_passed")) in {int, float}]),
            "turn_count_distribution": _distribution([
                len(row.get("turns")) for row in valid
                if isinstance(row.get("turns"), list)]),
            "changed_path_count_distribution": _distribution([
                row.get("changed_path_count") for row in valid
                if type(row.get("changed_path_count")) is int]),
            "native_write_request_count_distribution": _distribution([
                sum(item.get("native_write_request_count", 0)
                    for item in row.get("behavior_trace", ())
                    if isinstance(item, dict)
                    and type(item.get("native_write_request_count")) is int)
                for row in valid
                if isinstance(row.get("behavior_trace"), list)]),
            "validator_passes_by_criterion": {
                criterion: sum(bool(row.get("criterion_results", {}).get(criterion))
                               for row in valid)
                for criterion in CRITERIA
            },
            "scope_violation_trials": sum(
                bool(row.get("changed_paths_outside_src")) for row in valid),
            "mean_wall_time_ms": _mean([
                row["overall_wall_time_ms"] for row in valid
                if type(row.get("overall_wall_time_ms")) in (int, float)]),
            "median_wall_time_ms": _median([
                row["overall_wall_time_ms"] for row in valid
                if type(row.get("overall_wall_time_ms")) in (int, float)]),
            "wall_time_ms_distribution": _distribution([
                row.get("overall_wall_time_ms") for row in valid
                if type(row.get("overall_wall_time_ms")) in {int, float}]),
            "mean_model_tool_calls": _mean([
                row.get("model_tool_call_count", 0) for row in valid]),
            "model_tool_calls_distribution": _distribution([
                row.get("model_tool_call_count", 0) for row in valid]),
            "mean_validator_tool_calls": _mean([
                row.get("model_validator_call_count", 0) for row in valid]),
            "validator_tool_calls_distribution": _distribution([
                row.get("model_validator_call_count", 0) for row in valid]),
            "validator_channel_metrics": validator_channels,
            "completion_applicable_trials": len(completion_observed),
            "completion_permits": (sum(
                row["completion_permitted"] is True for row in completion_observed)
                if completion_observed else None),
            "completion_denials_or_incomplete": (sum(
                row["completion_permitted"] is False for row in completion_observed)
                if completion_observed else None),
            "cognitive_fault_trials": sum(
                bool(row.get("cognitive_fault")) for row in valid),
            "cfw_gate_blocks": sum(row.get("cfw_block_count", 0) for row in valid),
            "model_tool_call_failures": sum(
                not call.get("success", False)
                for row in valid for call in row.get("action_calls", ())),
            "token_usage_observed_trials": sum(
                row.get("token_usage_observed") is not None for row in valid),
        }
        if condition != "baseline":
            per_condition[condition]["integration_valid_trials"] = sum(
                row.get("integration_valid") is True for row in trials)
        per_condition[condition]["behavior_trace_outcomes"] = (
            _trace_outcome_summary(valid))
        if condition in {"triad", "triad-multipath"}:
            per_condition[condition]["mean_dp_interventions"] = _mean([
                status.get("dp_intervention_count", 0)
                for status in bridge_statuses])
            per_condition[condition]["dp_intervention_count_distribution"] = _distribution([
                status.get("dp_intervention_count") for status in bridge_statuses
                if type(status.get("dp_intervention_count")) in {int, float}])
            per_condition[condition]["dp_intervention_creations_by_origin"] = {
                origin: sum(
                    ((status.get("dp_intervention_metrics") or {}).get(
                        "created_by_origin") or
                     status.get("dp_intervention_creations_by_origin") or
                     status.get("dp_interventions_by_origin") or {}).get(origin, 0)
                    for status in bridge_statuses)
                for origin in ("startup", "runtime_behavior", "post_action", "completion")}
            normalized_names = (
                "unique_created", "active", "applications", "closed",
                "effective", "ineffective", "unknown")
            per_condition[condition]["dp_intervention_metric_totals"] = {
                name: sum(metrics.get(name, 0) for metrics in dp_intervention_metrics
                          if type(metrics.get(name)) is int)
                for name in normalized_names}
            per_condition[condition]["dp_intervention_metric_distributions"] = {
                name: _distribution([
                    metrics.get(name) for metrics in dp_intervention_metrics
                    if type(metrics.get(name)) is int])
                for name in normalized_names}
            per_condition[condition]["dp_intervention_origin_accounting_valid_trials"] = sum(
                type(metrics.get("unique_created")) is int
                and isinstance(metrics.get("created_by_origin"), Mapping)
                and sum(value for value in metrics["created_by_origin"].values()
                        if type(value) is int) == metrics["unique_created"]
                for metrics in dp_intervention_metrics)
            # Keep runtime counters as reported by the bridge, without deriving
            # high-level efficacy claims from the diagnostics.
            per_condition[condition]["dp_control_ticks_committed"] = sum(
                counter.get("control_ticks_committed", 0)
                for counter in cfw_metrics)
            per_condition[condition]["dp_reconciliations"] = sum(
                counter.get("reconciliations", 0) for counter in cfw_metrics)
            per_condition[condition]["dp_counter_distributions"] = {
                counter_name: _distribution([
                    counters.get(counter_name) for counters in cfw_metrics
                    if type(counters.get(counter_name)) in {int, float}])
                for counter_name in sorted({
                    name for counters in cfw_metrics for name in counters
                    if isinstance(name, str)})}

    baseline_by_pair = {row.get("pair_id"): row
                        for row in grouped.get("baseline", [])}
    paired = {}
    for condition in conditions:
        if condition == "baseline":
            continue
        by_pair = {row.get("pair_id"): row for row in grouped[condition]}
        deltas = []
        wins = ties = losses = 0
        completion_comparisons = []
        for pair in pairs:
            pair_id = pair["pair_id"]
            baseline = baseline_by_pair.get(pair_id)
            treatment = by_pair.get(pair_id)
            if (baseline is None or treatment is None
                    or baseline.get("efficacy_valid") is not True
                    or treatment.get("efficacy_valid") is not True):
                continue
            delta = (treatment.get("criteria_passed", 0)
                     - baseline.get("criteria_passed", 0))
            deltas.append(delta)
            wins += delta > 0
            losses += delta < 0
            ties += delta == 0
            completion_comparisons.append({
                "baseline_turn_completed": baseline.get("turn_status") == "completed",
                "treatment_turn_completed": treatment.get("turn_status") == "completed",
                "treatment_completion_permitted": treatment.get("completion_permitted"),
            })
        paired[condition] = {
            "pairs_with_both_efficacy_valid": len(deltas),
            "mean_criteria_delta_vs_baseline": _mean(deltas),
            "treatment_wins": wins, "ties": ties, "treatment_losses": losses,
            "completion_comparisons": completion_comparisons,
        }
    return {"conditions": per_condition, "matched_pairs": paired}


def _checkpoint(path: Path, report: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp",
                                     delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run matched generic Codex App Server coding trials across CFW conditions.")
    parser.add_argument("--trials", type=int, default=10,
                        help="matched pairs per condition (minimum 10)")
    parser.add_argument("--model", default=os.environ.get(
        "CODEX_EVAL_MODEL", "gpt-6-luna"))
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--auth-mode", choices=(
        "chatgpt", "openai-api-key", "external-provider"), default="chatgpt")
    parser.add_argument("--provider-id", default=None)
    parser.add_argument("--provider-base-url", default=None)
    parser.add_argument("--provider-env-key", default=None)
    parser.add_argument("--provider-wire-api", choices=(
        "responses", "chat_completions"), default="responses")
    parser.add_argument("--provider-env-file", type=Path, default=None,
                        help="private env file for the external provider key")
    parser.add_argument("--codex-binary", default=None,
                        help="packaged Codex CLI release binary")
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--behavioral-scenario", choices=tuple(SCENARIOS), default=None,
                        help="run one headroom pressure variant of the coding fixture")
    args = parser.parse_args()
    minimum_trials = 1 if args.behavioral_scenario else 10
    if args.trials < minimum_trials or args.trials > 100:
        parser.error(f"--trials must be between {minimum_trials} and 100")
    if not 1 <= args.max_turns <= 6:
        parser.error("--max-turns must be between 1 and 6")
    conditions = tuple(item.strip() for item in args.conditions.split(",")
                       if item.strip())
    if (len(set(conditions)) != len(conditions)
            or not set(conditions).issubset(CONDITIONS)
            or "baseline" not in conditions or len(conditions) < 2):
        parser.error("conditions must include baseline and at least one unique treatment")
    if not args.model.strip() or len(args.model) > 128:
        parser.error("model must be non-empty and at most 128 characters")
    if args.auth_mode == "external-provider" and not all((
            args.provider_id, args.provider_base_url, args.provider_env_key)):
        parser.error("external-provider mode requires provider id, base URL, and env key")

    csw_root = Path(os.environ.get("CSW_ROOT", ROOT.parent / "CognitiveStateWork"))
    dp_root = Path(os.environ.get("DP_ROOT", ROOT.parent / "DigitalPsychology"))
    repository_state = {
        "cfw": _repository_state(ROOT),
        "csw": _repository_state(csw_root),
        "dp": _repository_state(dp_root),
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    eval_prefix = (f"behavior-headroom-{args.behavioral_scenario}"
                   if args.behavioral_scenario else "coding-eval")
    output = args.output or (DEFAULT_OUTPUT_ROOT / f"{eval_prefix}-{stamp}.json")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.error(f"output already exists: {output}")

    report = {
        "evaluation_id": f"codex-app-server-coding-{stamp}",
        "fixture_id": "codex-app-server-coding-v1",
        "behavioral_scenario": args.behavioral_scenario,
        "model": args.model,
        "trials_per_condition": args.trials,
        "max_turns": args.max_turns,
        "auth_mode": args.auth_mode,
        "provider_id": args.provider_id,
        "provider_base_url": args.provider_base_url,
        "provider_env_key": args.provider_env_key,
        "conditions": list(conditions),
        "ordering_seed": args.seed,
        "repository_state": repository_state,
        "canonical_clean_commits": all(
            not item["working_tree_dirty"] for item in repository_state.values()),
        "source_sha256": {
            "fixture_files": _fixture_fingerprint(),
            "modular_codex_host_manifest": _codex_host_source_manifest(),
            "live_trial_runner": _sha256(TRIAL_RUNNER),
            "app_server_adapter": _sha256(ROOT / "scripts" / "codex_app_server_adapter.py"),
            "dp_bridge_adapter": _sha256(ROOT / "scripts" / "codex_app_server_dp_bridge.py"),
            "dp_host_bridge": _sha256(dp_root / "lib" / "host" / "bridge.py"),
            "dp_transaction_coordinator": _sha256(
                dp_root / "lib" / "host" / "transaction_coordinator.py"),
            "dp_projection_manager": _sha256(
                dp_root / "lib" / "host" / "projection_manager.py"),
        },
        "scope": (
            "Matched multi-defect coding task with host-owned criterion validators; "
            "scenario runs add bounded behavioral pressure, but reuse one fixture "
            "and do not establish broad coding-agent efficacy."),
        "internal_inference_boundaries_intercepted": False,
        "token_usage_note": (
            "Token usage is recorded only when the App Server reports it; the pilot "
            "did not expose tokenUsage or usage fields."),
        "trials": [],
    }
    rng = random.Random(args.seed)
    pairs: list[dict[str, str]] = []
    for pair_index in range(args.trials):
        pair_id = f"pair-{pair_index + 1:02d}"
        pair = {"pair_id": pair_id}
        pair.update({condition: f"coding-{stamp}-{pair_id}-{condition}"
                     for condition in conditions})
        pairs.append(pair)
        order = list(conditions)
        rng.shuffle(order)
        for condition in order:
            result = _one_trial(condition, pair[condition], args.model,
                                args.max_turns, auth_mode=args.auth_mode,
                                provider_id=args.provider_id,
                                provider_base_url=args.provider_base_url,
                                provider_env_key=args.provider_env_key,
                                provider_wire_api=args.provider_wire_api,
                                provider_env_file=args.provider_env_file,
                                codex_binary=args.codex_binary,
                                behavioral_scenario=args.behavioral_scenario)
            result["pair_id"] = pair_id
            report["trials"].append(result)
            report["summary"] = _summarize(report["trials"], pairs, conditions)
            _checkpoint(output, report)
            print(json.dumps({
                "pair_id": pair_id,
                "condition": condition,
                "trial_id": result.get("trial_id"),
                "trial_valid": result.get("trial_valid"),
                "host_integration_valid": result.get("host_integration_valid"),
                "inference_ready": result.get("inference_ready"),
                "model_infrastructure_valid": result.get(
                    "model_infrastructure_valid", result.get("infrastructure_valid")),
                "model_execution_observed": result.get("model_execution_observed"),
                "efficacy_valid": result.get("efficacy_valid"),
                "upstream_blocked": result.get("upstream_blocked"),
                "criteria_passed": result.get("criteria_passed"),
                "criteria_total": result.get("criteria_total"),
                "completion_permitted": result.get("completion_permitted"),
                "integration_valid": result.get("integration_valid"),
                "scope_violation_count": result.get("scope_violation_count"),
                "overall_wall_time_ms": result.get("overall_wall_time_ms"),
                "model_tool_call_count": result.get("model_tool_call_count"),
                "cognitive_fault": result.get("cognitive_fault"),
                "failure_class": result.get("failure_class"),
            }, sort_keys=True), flush=True)

    report["summary"] = _summarize(report["trials"], pairs, conditions)
    report["result_file"] = str(output)
    _checkpoint(output, report)
    print(json.dumps({"result_file": str(output), "summary": report["summary"]},
                     sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
