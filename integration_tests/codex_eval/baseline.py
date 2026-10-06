"""Unassisted direct Codex App Server baseline condition."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from codex_app_server_adapter import (
    CodexActionBinding,
    JsonlAppServerClient,
    _safe_error,
)
from codex_host.launch import CodexLaunchProfile
from codex_host.readiness import inspect_inference_readiness

from .report import _execution_failure_class, _failure_domain
from .scoring import _run_baseline_validator_set
from .validators import VALIDATOR_FILES


async def _run_baseline(*, workspace: Path, model: str, trial_id: str,
                        contract: Mapping[str, Any], actions: tuple[CodexActionBinding, ...],
                        validation_runs: list[dict[str, Any]],
                        action_calls: list[dict[str, Any]],
                        notifications: list[dict[str, Any]],
                        run_channel: dict[str, str], max_turns: int,
                        launch_profile: CodexLaunchProfile,
                        launch_environment: Mapping[str, str]) -> dict[str, Any]:
    action_by_name = {item.name: item for item in actions}
    criterion_by_validator = {
        str(item.get("validator")): str(item.get("id"))
        for item in contract.get("criteria", ())
        if isinstance(item, Mapping) and isinstance(item.get("validator"), str)
        and isinstance(item.get("id"), str)}
    completion = asyncio.get_running_loop().create_future()
    current_turn: dict[str, str | None] = {"id": None}
    early_completed: dict[str, dict[str, Any]] = {}
    last_message = ""
    behavior_action_trace: list[dict[str, Any]] = []
    last_unresolved_criterion_id: str | None = None
    wall_started = time.perf_counter()

    def notification(method: str, params: Mapping[str, Any]) -> None:
        nonlocal last_message
        event = {"method": method}
        if method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, Mapping):
                event["turn"] = dict(turn)
                completed_id = turn.get("id")
                if completed_id == current_turn["id"] and not completion.done():
                    completion.set_result(dict(turn))
                elif isinstance(completed_id, str) and len(early_completed) < 8:
                    early_completed[completed_id] = dict(turn)
        elif method == "item/completed":
            item = params.get("item")
            if isinstance(item, Mapping) and item.get("type") == "agentMessage":
                text = item.get("text")
                if isinstance(text, str):
                    last_message = text[:16000]
        elif method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                last_message = (last_message + delta)[-16000:]
        notifications.append(event)

    def request_handler(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal last_unresolved_criterion_id
        if method != "item/tool/call":
            return {"contentItems": [{"type": "inputText",
                                      "text": "Unsupported host request."}],
                    "success": False}
        name = str(params.get("tool") or "")
        binding = action_by_name.get(name)
        args = params.get("arguments")
        call_id = str(params.get("callId") or "")
        value: Any = None
        if binding is None or not isinstance(args, Mapping) or not call_id:
            result = {"contentItems": [{"type": "inputText",
                                        "text": "Invalid host tool request."}],
                      "success": False}
        else:
            try:
                value = binding.executor(dict(args))
                result = {"contentItems": [{"type": "inputText",
                                           "text": json.dumps(value, sort_keys=True)}],
                          "success": True}
            except Exception as exc:  # noqa: BLE001 - return sanitized host tool failures
                result = {"contentItems": [{"type": "inputText",
                                           "text": f"Host tool failed: {type(exc).__name__}"}],
                          "success": False}
        action_calls.append({"tool": name, "call_id": call_id,
                             "success": result.get("success") is True,
                             "validator": (args.get("validator") if isinstance(args, Mapping)
                                           else None)})
        validator = (args.get("validator")
                     if isinstance(args, Mapping)
                     and isinstance(args.get("validator"), str) else None)
        criterion_id = criterion_by_validator.get(validator or "")
        passed = (value.get("status") == "passed"
                  if isinstance(value, Mapping) and validator else None)
        raw_path = args.get("path") if isinstance(args, Mapping) else None
        normalized_path = (raw_path.replace("\\", "/")
                           if isinstance(raw_path, str) else "")
        path_validator = next((name for name, test_path in VALIDATOR_FILES.items()
                               if normalized_path == test_path
                               or normalized_path.endswith(f"/{test_path}")), None)
        if criterion_id:
            target_criterion_id = criterion_id
            target_source = "validator_call"
        elif path_validator:
            target_criterion_id = criterion_by_validator.get(path_validator)
            target_source = "test_path"
        elif last_unresolved_criterion_id:
            target_criterion_id = last_unresolved_criterion_id
            target_source = "last_failed_validator"
        else:
            target_criterion_id = None
            target_source = "unattributed"
        if criterion_id and passed is True and criterion_id == last_unresolved_criterion_id:
            last_unresolved_criterion_id = None
        elif criterion_id and passed is False:
            last_unresolved_criterion_id = criterion_id
        behavior_action_trace.append({
            "action_index": len(behavior_action_trace) + 1,
            "turn_index": len(turn_records) + 1,
            "elapsed_ms": round((time.perf_counter() - wall_started) * 1000, 3),
            "action_class": {
                "cfw_read": "read", "cfw_write": "write",
                "cfw_run_validator": "validator",
            }.get(name, "other"),
            "criterion_ids": [target_criterion_id] if target_criterion_id else [],
            "target_criterion_id": target_criterion_id,
            "target_source": target_source,
            "focus_criterion_ids": [target_criterion_id] if target_criterion_id else [],
            "criterion_progress_delta": int(passed is True),
            "progress_deltas": ({target_criterion_id: 1}
                                 if target_criterion_id and passed is True
                                 else {}),
            "result_class": ("failure" if passed is False
                             or result.get("success") is not True else "success"),
            "same_approach_retry_count": 0,
            "equivalent_retry_count": 0,
        })
        return result

    client = await JsonlAppServerClient.launch(
        cwd=workspace, launch_profile=launch_profile,
        launch_environment=launch_environment)
    client.set_handlers(request_handler=request_handler,
                        notification_handler=notification)
    try:
        await client.request("initialize", {
            "clientInfo": {"name": "cfw-coding-eval",
                           "title": "Generic Codex Coding Evaluation",
                           "version": "1.0.0"},
            "capabilities": {"experimentalApi": True},
        })
        await client.notify("initialized")
        readiness = await inspect_inference_readiness(client, launch_profile)
        thread = await client.request("thread/start", {
            "model": model, "cwd": str(workspace), "approvalPolicy": "never",
            "sandbox": "read-only",
            "dynamicTools": [item.app_server_spec() for item in actions],
            "developerInstructions": (
                "Use only the host-provided cfw_ tools for task actions. "
                "The host owns the task contract and test results. "
                "Do not claim completion without reporting host validator results."),
        })
        thread_info = thread.get("thread") if isinstance(thread, Mapping) else None
        thread_id = thread_info.get("id") if isinstance(thread_info, Mapping) else None
        session_id = thread_info.get("sessionId") if isinstance(thread_info, Mapping) else None
        if not isinstance(thread_id, str) or not isinstance(session_id, str):
            raise TypeError("Codex App Server omitted baseline transport identities")

        criteria = contract["criteria"]
        contract_context = "\n".join(
            [f"Objective: {contract['objective']}",
             f"Deliverable: {contract['deliverable_kind']}",
             "Host-owned acceptance criteria:"]
            + [f"- {item['id']}: {item['claim']}" for item in criteria])
        base_prompt = (f"{contract['task_prompt']}\n\n"
                       f"Task workspace: {workspace}\n"
                       f"Authorized source directory: {workspace / 'src'}")
        final_results: list[dict[str, Any]] = []
        turn_records: list[dict[str, Any]] = []
        feedback = ""
        for turn_index in range(max_turns):
            run_channel["value"] = "model_turn"
            text = base_prompt if not feedback else f"{base_prompt}\n\n{feedback}"
            scenario_followup = contract.get("behavioral_followup")
            if (turn_index > 0 and isinstance(scenario_followup, str)
                    and scenario_followup):
                text += f"\n\nUSER FOLLOW-UP\n{scenario_followup}"
            full_input = (f"{text}\n\nHOST TASK CONTRACT\n{contract_context}"
                          "\n\nCFW RUNTIME GUIDANCE\n")
            if completion.done():
                raise RuntimeError("stale baseline turn-completion signal")
            response_future = completion
            start_time = time.perf_counter()
            response = await client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": full_input}],
                "cwd": str(workspace), "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
            })
            turn = response.get("turn") if isinstance(response, Mapping) else None
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise RuntimeError("Codex App Server omitted a baseline turn identity")
            current_turn["id"] = turn_id
            early = early_completed.pop(turn_id, None)
            if early is not None and not response_future.done():
                response_future.set_result(early)
            if response_future.done():
                done_turn = response_future.result()
                if done_turn.get("id") == turn_id:
                    turn_result = done_turn
                else:
                    completion = asyncio.get_running_loop().create_future()
                    turn_result = await asyncio.wait_for(completion, timeout=600)
            else:
                turn_result = await asyncio.wait_for(response_future, timeout=600)
            completion = asyncio.get_running_loop().create_future()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 3)
            turn_records.append({"turn_id": turn_id,
                                 "status": turn_result.get("status"),
                                 "error": _safe_error(turn_result.get("error")),
                                 "wall_time_ms": elapsed_ms})
            if turn_result.get("status") != "completed":
                break
            run_channel["value"] = "baseline_host_feedback"
            final_results = _run_baseline_validator_set(
                workspace, validation_runs=validation_runs,
                channel="baseline_host_feedback")
            failing = [item["validator"] for item in final_results
                       if item["status"] != "passed"]
            if not failing:
                break
            if turn_index + 1 < max_turns:
                feedback = ("Host test results after your last turn: failing validators: "
                            + ", ".join(failing) + ". Read the exact failing tests, "
                            "make source-only changes, then rerun the affected validators.")
        wall_ms = round((time.perf_counter() - wall_started) * 1000, 3)
        model_execution_observed = bool(action_calls or last_message)
        failure_class = _execution_failure_class(
            turn_records, model_execution_observed=model_execution_observed)
        return {"condition": "baseline", "stack": "host-tools-baseline",
                "trial_id": trial_id, "model": model,
                "inference_ready": readiness.ready,
                "model_execution_observed": model_execution_observed,
                "efficacy_scored": bool(model_execution_observed and final_results),
                "failure_domain": _failure_domain(failure_class),
                "infrastructure_valid": bool(readiness.ready
                                              and model_execution_observed),
                "model_infrastructure_valid": bool(readiness.ready
                                                     and model_execution_observed),
                "failure_class": failure_class,
                "host_integration_valid": bool(
                    readiness.ready and client.host_policy_verified),
                "turns": turn_records, "turn_status": turn_records[-1]["status"]
                    if turn_records else "not_started",
                "behavior_trace": [],
                "behavior_action_trace": behavior_action_trace[-512:],
                "required_criterion_ids": sorted(criterion_by_validator.values()),
                "assistant_message": last_message[-4000:],
                "final_validator_results": final_results,
                "completion_permitted": None,
                "overall_wall_time_ms": wall_ms,
                "app_server_thread_id": thread_id,
        "app_server_session_id": session_id,
                "integration_valid": bool(turn_records
                    and turn_records[-1]["status"] == "completed"),
                "internal_inference_boundaries_intercepted": False}
    finally:
        await client.close()
