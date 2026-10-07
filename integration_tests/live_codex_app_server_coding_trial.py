from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# Set the optional StateWork provider root before importing CFW modules that
# freeze their runtime asset paths at module import time.
from codex_app_server_adapter import _safe_error
from codex_eval import common as _codex_eval_common  # noqa: F401
from codex_eval.actions import _make_actions
from codex_eval.baseline import _run_baseline
from codex_eval.behavior_scenarios import SCENARIOS, prepare_behavior_scenario
from codex_eval.fixture import (
    _load_contract,
    _materialize_workspace,
    _source_digest,
    _tracked_files,
)
from codex_eval.launch import (
    _inference_preflight,
    _launch_profile,
    prepare_codex_home,
)
from codex_eval.report import (
    _app_server_error_events,
    _app_server_item_counts,
    _failure_domain,
    _record_app_server_notification,
    _sanitized_trial_summary,
    _validator_channel_metrics,
)
from codex_eval.scoring import _run_baseline_validator_set
from codex_eval.treatment import _run_treatment
from codex_eval.validators import _validator_definitions

__all__ = ["_app_server_error_events", "_record_app_server_notification"]


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _task_binding_digests(contract: Mapping[str, Any], actions: Any,
                          validators: Any) -> dict[str, str]:
    """Hash the actual task and host binding contracts used by this trial."""
    action_bindings = []
    for action in actions:
        action_bindings.append({
            "name": action.name,
            "description": action.description,
            "app_server_spec": action.app_server_spec(),
            "action_binding_id": action.action_binding_id,
            "descriptor": action.descriptor.__dict__,
        })
    validator_bindings = []
    for validator in validators:
        validator_bindings.append({
            "validator_id": validator.validator_id,
            "action_binding_id": validator.action_binding_id,
            "arguments": validator.arguments_value(),
            "criterion_ids": list(validator.criterion_ids),
            "covered_boundary_ids": list(validator.covered_boundary_ids),
            "result_schema": validator.result_schema_value(),
        })
    return {
        "task_contract_hash": _canonical_digest(contract),
        "tool_binding_hash": _canonical_digest(action_bindings),
        "validator_binding_hash": _canonical_digest(validator_bindings),
    }


def _initialize_workspace_git(workspace: Path) -> None:
    """Seed the disposable workspace with the machine's canonical Git identity."""
    user_name = subprocess.check_output(
        ["git", "config", "--global", "--get", "user.name"], text=True).strip()
    user_email = subprocess.check_output(
        ["git", "config", "--global", "--get", "user.email"], text=True).strip()
    if not user_name or not user_email:
        raise RuntimeError("canonical Git identity is unavailable")
    commands = (
        ("git", "init", "-q"),
        ("git", "config", "user.name", user_name),
        ("git", "config", "user.email", user_email),
        ("git", "add", "."),
        ("git", "commit", "-qm", "seed coding evaluation fixture"),
    )
    for command in commands:
        subprocess.run(command, cwd=workspace, check=True)


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    if args.condition not in {
            "baseline", "cfw", "cfw-csw", "triad", "triad-multipath"}:
        raise ValueError("unsupported evaluation condition")
    if not args.trial_id or len(args.trial_id) > 128:
        raise ValueError("trial-id must be non-empty and at most 128 characters")
    if not args.model.strip() or len(args.model) > 128:
        raise ValueError("model must be non-empty and at most 128 characters")
    preflight = await _inference_preflight(args)
    if args.inference_preflight_only or preflight.get("inference_ready") is not True:
        return preflight
    dynamic_tool_probe = bool(args.dynamic_tool_probe)
    contract = ({
        "objective": "Read the probe marker and return it exactly.",
        "deliverable_kind": "analysis", "work_mode": "research",
        "task_complexity": "trivial", "non_goals": (),
        "task_prompt": ("Use cfw_read to read probe.txt, then reply with exactly "
                        "the marker contained in that file. Do not use another tool."),
        "criteria": [{"id": "probe_marker", "claim": "The marker was read and returned.",
                      "validator": "probe_marker"}],
    } if dynamic_tool_probe else _load_contract())
    validation_runs: list[dict[str, Any]] = []
    action_calls: list[dict[str, Any]] = []
    notifications: list[dict[str, Any]] = []
    run_channel = {"value": "model_turn"}
    with tempfile.TemporaryDirectory(prefix="codex-appserver-coding-") as raw:
        root = Path(raw)
        launch_profile, launch_environment = _launch_profile(
            args, root / "codex-home")
        prepare_codex_home(launch_profile, model=args.model,
                           parent_environment=os.environ,
                           api_key=os.environ.get("OPENAI_API_KEY"))
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        _materialize_workspace(workspace)
        contract = prepare_behavior_scenario(
            workspace, contract, args.behavioral_scenario)
        if dynamic_tool_probe:
            (workspace / "src" / "probe.txt").write_text(
                "CFW_PROBE_OK_2026\n", encoding="utf-8")
        _initialize_workspace_git(workspace)

        actions = _make_actions(workspace, validation_runs=validation_runs,
                                run_channel=run_channel)
        validators, criterion_by_validator = _validator_definitions(contract)
        task_binding_digests = _task_binding_digests(contract, actions, validators)
        if dynamic_tool_probe:
            actions = actions[:1]
            validators = ()
            result = await _run_treatment(
                condition="triad", workspace=workspace, root=root,
                model=args.model, trial_id=args.trial_id, contract=contract,
                actions=actions, validators=validators,
                validation_runs=validation_runs, action_calls=action_calls,
                notifications=notifications, run_channel=run_channel,
                max_turns=1, launch_profile=launch_profile,
                launch_environment=launch_environment,
                dynamic_tool_probe=True)
        elif args.condition == "baseline":
            run_channel["value"] = "model_turn"
            result = await _run_baseline(
                workspace=workspace, model=args.model, trial_id=args.trial_id,
                contract=contract, actions=actions,
                validation_runs=validation_runs, action_calls=action_calls,
                notifications=notifications, run_channel=run_channel,
                max_turns=args.max_turns, launch_profile=launch_profile,
                launch_environment=launch_environment)
            if (not result.get("final_validator_results")
                    and result.get("model_execution_observed") is True):
                result["final_validator_results"] = _run_baseline_validator_set(
                    workspace, validation_runs=validation_runs,
                    channel="external_final_score")
        else:
            result = await _run_treatment(
                condition=args.condition, workspace=workspace, root=root,
                model=args.model, trial_id=args.trial_id, contract=contract,
                actions=actions, validators=validators,
                validation_runs=validation_runs, action_calls=action_calls,
                notifications=notifications, run_channel=run_channel,
                max_turns=args.max_turns, launch_profile=launch_profile,
                launch_environment=launch_environment,
                boundary_probe=args.boundary_probe)

        changed_paths = _tracked_files(workspace)
        changed_outside_scope = sorted(
            item for item in changed_paths if not item.startswith("src/"))
        app_server_item_type_counts, app_server_item_tool_counts = (
            _app_server_item_counts(notifications))
        validator_passes = {
            str(item["validator"]): item.get("status") == "passed"
            for item in result.get("final_validator_results", ())
        }
        efficacy_scored = result.get("efficacy_scored") is True
        model_execution_observed = result.get("model_execution_observed") is True
        fixture_state_after_failed_start = ({
            "source_sha256": _source_digest(workspace),
            "changed_path_count": len(changed_paths),
            "validator_results": [
                {"validator": str(item.get("validator") or ""),
                 "status": str(item.get("status") or "")}
                for item in validation_runs
                if isinstance(item, Mapping)
                and item.get("status") in {"passed", "failed"}
            ],
        } if not model_execution_observed else None)
        result.update({
            **task_binding_digests,
            "behavioral_scenario": contract.get("behavioral_scenario"),
            "required_criterion_ids": sorted(criterion_by_validator.values()),
            "action_calls": action_calls,
            "model_tool_call_count": len(action_calls),
            "model_validator_call_count": sum(
                item.get("tool") == "cfw_run_validator" for item in action_calls),
            "validator_run_records": validation_runs,
            "validator_channel_metrics": _validator_channel_metrics(validation_runs),
            "changed_paths": changed_paths,
            "changed_paths_outside_src": changed_outside_scope,
            "source_sha256_final": _source_digest(workspace),
            "criterion_results": {
                criterion_by_validator[validator_id]: passed
                for validator_id, passed in validator_passes.items()
                if efficacy_scored and validator_id in criterion_by_validator
            },
            "criteria_passed": (sum(validator_passes.values())
                                 if efficacy_scored else None),
            "criteria_total": (len(criterion_by_validator)
                                if efficacy_scored else None),
            "fixture_state_after_failed_start": fixture_state_after_failed_start,
            "inference_ready": result.get("inference_ready") is True,
            "model_execution_observed": model_execution_observed,
            "efficacy_scored": efficacy_scored,
            "failure_domain": result.get("failure_domain"),
            "infrastructure_valid": result.get("infrastructure_valid") is True,
            "model_infrastructure_valid": result.get(
                "model_infrastructure_valid", result.get("infrastructure_valid")) is True,
            "execution_phase": result.get("execution_phase"),
            "scope_violation_count": len(changed_outside_scope),
            "cfw_block_count": sum(
                1 for item in result.get("cfw_gate_receipts", ())
                if item.get("decision") not in {"allow", "authorized"}),
            "app_server_event_counts": {
                method: sum(item.get("method") == method for item in notifications)
                for method in ("turn/started", "turn/completed", "error", "item/started",
                               "item/completed", "item/agentMessage/delta")
            },
            "app_server_item_type_counts": app_server_item_type_counts,
            "app_server_item_tool_counts": app_server_item_tool_counts,
            "token_usage_observed": next((
                item["turn"].get("tokenUsage") or item["turn"].get("usage")
                for item in reversed(notifications)
                if isinstance(item.get("turn"), Mapping)
                and (item["turn"].get("tokenUsage") is not None
                     or item["turn"].get("usage") is not None)), None),
        })
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one generic Codex App Server multi-defect coding trial.")
    parser.add_argument("--condition", choices=(
        "baseline", "cfw", "cfw-csw", "triad", "triad-multipath"),
                        required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--model", default=os.environ.get("CODEX_EVAL_MODEL", "gpt-6-luna"))
    parser.add_argument("--auth-mode", choices=(
        "chatgpt", "openai-api-key", "external-provider"), default="chatgpt")
    parser.add_argument("--provider-id", default=None)
    parser.add_argument("--provider-base-url", default=None)
    parser.add_argument("--provider-env-key", default=None)
    parser.add_argument("--provider-wire-api", choices=(
        "responses", "chat_completions"), default="responses")
    parser.add_argument("--provider-env-file", type=Path, default=None,
                        help="private env file for the external provider key")
    parser.add_argument("--auth-source-home", type=Path, default=None,
                        help="Codex auth source for the temporary ChatGPT profile")
    parser.add_argument("--inference-preflight-only", action="store_true")
    parser.add_argument("--dynamic-tool-probe", action="store_true",
                        help="run the minimal CFW cfw_read + DP action proof")
    parser.add_argument("--max-turns", type=int, default=3,
                        help="maximum repair/validator-feedback turns (1-6)")
    parser.add_argument("--boundary-probe", action="store_true",
                        help="exercise one failing read through the host boundaries without a model turn")
    parser.add_argument("--behavioral-scenario", choices=tuple(SCENARIOS), default=None,
                        help="apply one bounded behavioral headroom scenario")
    parser.add_argument("--sanitized-json", action="store_true",
                        help="print a bounded result summary without model text or tool payloads")
    args = parser.parse_args()
    if args.max_turns < 1 or args.max_turns > 6:
        parser.error("--max-turns must be between 1 and 6")
    if args.auth_mode == "external-provider" and not all((
            args.provider_id, args.provider_base_url, args.provider_env_key)):
        parser.error("external-provider mode requires --provider-id, "
                     "--provider-base-url, and --provider-env-key")
    if args.dynamic_tool_probe and args.condition != "triad":
        parser.error("--dynamic-tool-probe requires --condition triad")
    if args.dynamic_tool_probe and args.behavioral_scenario:
        parser.error("--dynamic-tool-probe cannot be combined with a behavioral scenario")
    try:
        result = asyncio.run(main_async(args))
    except Exception as exc:  # noqa: BLE001 - sanitized CLI failure report is fail-closed
        if not args.sanitized_json:
            traceback.print_exc(limit=8, file=sys.stderr)
        failure_message = _safe_error({"message": str(exc)}).get("message")
        normalized = str(failure_message or "").lower()
        failure_class = ("workspace_routing_unavailable"
                         if "workspace routing" in normalized
                         else type(exc).__name__)
        failure = {"condition": args.condition, "trial_id": args.trial_id,
                   "model": args.model, "integration_valid": False,
                   "inference_ready": False, "model_execution_observed": False,
                   "efficacy_scored": False, "infrastructure_valid": False,
                   "model_infrastructure_valid": False,
                   "host_integration_valid": False,
                   "failure_domain": _failure_domain(failure_class),
                   "failure_class": failure_class,
                   "failure_message": failure_message}
        output = _sanitized_trial_summary(failure) if args.sanitized_json else failure
        print(json.dumps(output, sort_keys=True))
        return 1
    output = _sanitized_trial_summary(result) if args.sanitized_json else result
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
