"""CFW/CSW/DP treatment condition over the disposable coding fixture."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from codex_app_server_adapter import (
    CodexActionBinding,
    CodexValidatorBinding,
    JsonlAppServerClient,
)
from codex_host.launch import CodexLaunchProfile

from .csw_setup import (
    _complete_getter_workflow,
    _complete_gitter_workflow,
    _csw_selection_snapshot,
)
from .report import (
    _app_server_error_events,
    _execution_failure_class,
    _failure_domain,
)
from .scoring import _run_baseline_validator_set
from .triad_setup import _build_treatment_host
from .validators import VALIDATOR_FILES


async def _run_treatment(*, condition: str, workspace: Path, root: Path,
                         model: str, trial_id: str, contract: Mapping[str, Any],
                         actions: tuple[CodexActionBinding, ...],
                         validators: tuple[CodexValidatorBinding, ...],
                         validation_runs: list[dict[str, Any]],
                         action_calls: list[dict[str, Any]],
                         notifications: list[dict[str, Any]],
                         run_channel: dict[str, str], max_turns: int,
                         launch_profile: CodexLaunchProfile,
                         launch_environment: Mapping[str, str],
                         boundary_probe: bool = False,
                         dynamic_tool_probe: bool = False) -> dict[str, Any]:
    setup = _build_treatment_host(
        condition=condition, workspace=workspace, root=root, model=model,
        trial_id=trial_id, contract=contract, actions=actions,
        validators=validators, notifications=notifications,
        launch_profile=launch_profile, launch_environment=launch_environment,
        dynamic_tool_probe=dynamic_tool_probe)
    adapter = setup.adapter
    request = setup.request
    authority = setup.authority
    with_csw = setup.with_csw
    with_dp = setup.with_dp
    requested_statework_ids = setup.requested_statework_ids
    cfw_gate_receipts = setup.cfw_gate_receipts
    dp_observation_records = setup.dp_observation_records
    csw_workflow_identities = setup.csw_workflow_identities
    csw_workflow_results = setup.csw_workflow_results
    getter_prepared = setup.getter_prepared
    cognitive_host = setup.cognitive_host
    server_request_counts = setup.server_request_counts
    completion_results: list[dict[str, Any]] = []
    completion_attempts: list[dict[str, Any]] = []
    turn_records: list[dict[str, Any]] = []
    behavior_action_trace: list[dict[str, Any]] = []
    criterion_ids = tuple(
        str(item.get("id")) for item in contract.get("criteria", ())
        if isinstance(item, Mapping) and isinstance(item.get("id"), str))
    validator_criteria = {
        str(binding.arguments.get("validator")): tuple(binding.criterion_ids)
        for binding in validators
        if isinstance(binding.arguments, Mapping)
        and isinstance(binding.arguments.get("validator"), str)}
    criterion_progress = {criterion_id: False for criterion_id in criterion_ids}
    last_unresolved_criterion_id: str | None = None
    feedback = ""
    wall_started = time.perf_counter()
    workflow_ids: dict[str, Any] = {}
    thread_status: Mapping[str, Any] = {}

    def record_completion_attempt(result: Mapping[str, Any]) -> None:
        validators = result.get("validators")
        safe_validators = []
        if isinstance(validators, (tuple, list)):
            for item in validators[:32]:
                if not isinstance(item, Mapping):
                    continue
                safe_validators.append({
                    "validator_id": item.get("validator_id"),
                    "criterion_ids": list(item.get("criterion_ids") or ()),
                    "passed": item.get("passed") is True,
                    "reused": item.get("reused") is True,
                    "evidence_ref": str(item.get("evidence_ref") or "")[:256],
                    "result_digest": str(item.get("result_digest") or "")[:64],
                    "reuse_basis_digest": str(
                        item.get("reuse_basis_digest") or "")[:64],
                })
        completion_attempts.append({
            "attempt": len(completion_attempts) + 1,
            "status": result.get("status"),
            "decision": result.get("decision"),
            "validators": safe_validators,
        })

    try:
        client = await JsonlAppServerClient.launch(
            cwd=workspace, launch_profile=launch_profile,
            launch_environment=launch_environment)
        thread_status = await adapter.open(client)

        if boundary_probe:
            probe = await adapter._handle_dynamic_tool({
                "tool": "cfw_read", "arguments": {"path": "missing-probe-target"},
                "callId": "boundary-probe-failed-read",
            })
            probe_status = adapter.status()
            gate_receipt_summary = []
            for item in cfw_gate_receipts[:8]:
                receipt = item.get("gate_receipt")
                reason_codes = (receipt.get("reason_codes")
                                if isinstance(receipt, Mapping) else ())
                gate_receipt_summary.append({
                    "stage": str(item.get("stage") or "")[:32],
                    "decision": str(item.get("decision") or "")[:64],
                    "reason_codes": [str(code)[:128] for code in reason_codes[:8]
                                     if isinstance(code, str)]
                    if isinstance(reason_codes, (tuple, list)) else [],
                })
            probe_bridge = probe_status.get("cognitive_host_status") or {}
            probe_metrics = probe_bridge.get("production_metrics") or {}
            probe_counters = probe_metrics.get("counters") or {}
            probe_csw_selection = _csw_selection_snapshot(
                authority, csw_workflow_identities)
            supported_native_features = set(
                probe_status.get("native_action_features_supported") or ())
            disabled_native_features = set(
                probe_status.get("native_action_features_disabled") or ())
            probe_invariants = {
                "app_server_connected": probe_status.get("connected") is True,
                "model_visible_dp_tools_zero": probe_status.get(
                    "model_visible_dp_tools") == 0,
                "model_visibility_verified": probe_status.get(
                    "model_visibility_verified") is True,
                "native_mutation_surfaces_closed": (
                    probe_status.get("native_mutation_surfaces_closed") is True
                    and supported_native_features.issubset(disabled_native_features)
                    and bool(disabled_native_features.intersection(
                        {"shell_tool", "unified_exec"}))),
                "combined_enforcing": probe_status.get("runtime_mode")
                    == "combined_enforcing",
                "private_dp_bridge_open": probe_bridge.get(
                    "private_dp_bridge_open") is True,
                "cfw_authority_verified": probe_bridge.get(
                    "cfw_authority_verified") is True,
                "shared_csw_authority_verified": probe_bridge.get(
                    "shared_csw_authority_verified") is True,
                "dp_fault_free": probe_bridge.get("dp_faulted") is False,
                "dp_behavior_revision_advanced": probe_bridge.get(
                    "dp_behavior_revision", 0) >= 1,
                "control_ticks_committed": probe_counters.get(
                    "control_ticks_committed", 0) >= 3,
                "csw_directive_rejections_zero": probe_counters.get(
                    "csw_directive_rejections", 0) == 0,
                "cfw_preflight_allowed": any(
                    item["stage"] == "preflight" and item["decision"] == "allow"
                    for item in gate_receipt_summary),
                "cfw_final_gate_allowed": any(
                    item["stage"] == "final" and item["decision"] == "allow"
                    for item in gate_receipt_summary),
                "dp_post_action_observed": any(
                    item.get("boundary") == "post_action"
                    for item in dp_observation_records),
                "csw_selection_revision_advanced": (
                    condition != "triad-multipath" or
                    isinstance(probe_csw_selection, Mapping)
                    and probe_csw_selection.get("selection_revision", 0) >= 1),
                "getter_selected_by_csw": (
                    condition != "triad-multipath" or
                    isinstance(probe_csw_selection, Mapping)
                    and "getter" in probe_csw_selection.get("active_workflow_ids", ())),
                "gitter_retained_for_getter": (
                    condition != "triad-multipath" or
                    isinstance(probe_csw_selection, Mapping)
                    and probe_csw_selection.get("dependency_required_by", {}).get(
                        "gitter") == ["getter"]),
                "expected_missing_read_failed": (
                    probe.get("success") is False
                    and any("Host tool execution failed." in str(item.get("text") or "")
                            for item in probe.get("contentItems", ())
                            if isinstance(item, Mapping))),
            }
            return {
                "condition": condition, "trial_id": trial_id, "model": model,
                "boundary_probe": probe,
                "cognitive_fault": probe_status.get("cognitive_fault"),
                "cognitive_fault_detail": probe_status.get("cognitive_fault_detail"),
                "cognitive_host_status": probe_bridge,
                "cfw_gate_receipts": gate_receipt_summary,
                "dp_observation_records": dp_observation_records,
                "csw_selection_snapshot": probe_csw_selection,
                "runtime_mode": probe_status.get("runtime_mode"),
                "behavior_action_trace": [],
                "native_action_features_supported": sorted(supported_native_features),
                "native_action_features_disabled": sorted(disabled_native_features),
                "native_action_feature_categories": probe_status.get(
                    "native_action_feature_categories"),
                "boundary_probe_invariants": probe_invariants,
                "boundary_probe_valid": all(probe_invariants.values()),
                # A no-model probe proves host boundaries only; it is not a
                # completed evaluation trial and must never count as one.
                "integration_invariants": {}, "integration_valid": False,
                "internal_inference_boundaries_intercepted": False,
            }

        async def record_action_call(method: str,
                                     params: Mapping[str, Any]) -> Any:
            nonlocal last_unresolved_criterion_id
            server_request_counts[method] += 1
            tool_name = str(params.get("tool") or "")
            arguments = params.get("arguments")
            validator_id = (
                arguments.get("validator")
                if isinstance(arguments, Mapping)
                and isinstance(arguments.get("validator"), str) else None)
            turn_trace = (adapter._behavior_trace[-1]
                          if adapter._behavior_trace else {})
            focus_ids = tuple(
                criterion_id for criterion_id in turn_trace.get(
                    "focus_criterion_ids", ())
                if isinstance(criterion_id, str) and criterion_id in criterion_progress
            ) if isinstance(turn_trace, Mapping) else ()
            action_criteria = (validator_criteria.get(validator_id, ())
                               if validator_id else ())
            path_value = (arguments.get("path")
                          if isinstance(arguments, Mapping) else None)
            normalized_path = (path_value.replace("\\", "/")
                               if isinstance(path_value, str) else "")
            path_criterion = next((validator_criteria[validator]
                                   for validator, test_path in VALIDATOR_FILES.items()
                                   if validator in validator_criteria
                                   and (normalized_path == test_path
                                        or normalized_path.endswith(
                                            f"/{test_path}"))), None)
            if action_criteria:
                target_criterion_id = action_criteria[0]
                target_source = "validator_call"
            elif path_criterion is not None:
                target_criterion_id = path_criterion[0]
                target_source = "test_path"
            elif focus_ids:
                target_criterion_id = focus_ids[0]
                target_source = "host_focus"
            elif last_unresolved_criterion_id:
                target_criterion_id = last_unresolved_criterion_id
                target_source = "last_failed_validator"
            else:
                target_criterion_id = None
                target_source = "unattributed"
            validations_before = len(validation_runs)
            result = await adapter.handle_server_request(method, params)
            if method == "item/tool/call":
                action_calls.append({
                    "tool": tool_name,
                    "call_id": str(params.get("callId") or ""),
                    "success": result.get("success") is True,
                    "validator": validator_id,
                })
                progress_deltas: dict[str, int] = {}
                criterion_validation_passed: bool | None = None
                if validator_id and len(validation_runs) > validations_before:
                    validation = validation_runs[-1]
                    if validation.get("validator") == validator_id:
                        passed = validation.get("status") == "passed"
                        if passed and last_unresolved_criterion_id in action_criteria:
                            last_unresolved_criterion_id = None
                        elif not passed:
                            last_unresolved_criterion_id = action_criteria[0]
                        criterion_validation_passed = passed
                        for criterion_id in action_criteria:
                            prior = criterion_progress.get(criterion_id, False)
                            if passed and not prior:
                                progress_deltas[criterion_id] = 1
                                criterion_progress[criterion_id] = True
                            elif not passed and prior:
                                progress_deltas[criterion_id] = -1
                                criterion_progress[criterion_id] = False
                counters = turn_trace.get("retry_recovery_counters", {})
                retry_count = (counters.get("same_approach_retries", 0)
                               if isinstance(counters, Mapping) else 0)
                equivalent_retry_count = (
                    counters.get("equivalent_retry_count", 0)
                    if isinstance(counters, Mapping) else 0)
                behavior_action_trace.append({
                    "action_index": len(behavior_action_trace) + 1,
                    "turn_index": len(turn_records) + 1,
                    "elapsed_ms": round(
                        (time.perf_counter() - wall_started) * 1000, 3),
                    "tool": tool_name,
                    "action_class": {
                        "cfw_read": "read", "cfw_write": "write",
                        "cfw_run_validator": "validator",
                    }.get(tool_name, "other"),
                    "criterion_ids": list(action_criteria or focus_ids)[:8],
                    "target_criterion_id": target_criterion_id,
                    "target_source": target_source,
                    "focus_criterion_ids": list(focus_ids)[:8],
                    "criterion_progress_delta": sum(progress_deltas.values()),
                    "progress_deltas": progress_deltas,
                    "result_class": (
                        "success" if result.get("success") is True
                        and criterion_validation_passed is not False else "failure"),
                    "behavioral_mode": (turn_trace.get("behavioral_mode")
                                        if isinstance(turn_trace.get(
                                            "behavioral_mode"), str) else ""),
                    "same_approach_retry_count": (
                        retry_count if type(retry_count) is int
                        and 0 <= retry_count <= 1_000_000 else 0),
                    "equivalent_retry_count": (
                        equivalent_retry_count
                        if type(equivalent_retry_count) is int
                        and 0 <= equivalent_retry_count <= 1_000_000 else 0),
                    "redirected": turn_trace.get("redirected") is True,
                })
            return result
        client.set_handlers(request_handler=record_action_call,
                            notification_handler=adapter.handle_notification)
        if with_csw:
            workflow_ids = dict(adapter._task_handle.digital_psychology_adapter()
                                .workflow_authority_identities())
            csw_workflow_identities.update({
                str(statework_id): {**dict(identity),
                                    "statework_id": str(statework_id)}
                for statework_id, identity in workflow_ids.items()})
        if (condition == "triad-multipath"
                and set(workflow_ids) != {"gitter", "getter"}):
            raise RuntimeError(
                "host request did not resolve Getter with its Gitter dependency")
        base_prompt = (("Use the host cfw_read tool to read src/probe.txt. "
                        "Reply with exactly the marker contained in that file. "
                        "Do not use any other tool.") if dynamic_tool_probe else
                       f"{contract['task_prompt']}\n\n"
                       f"Task workspace: {workspace}\n"
                       f"Authorized source directory: {workspace / 'src'}")
        for turn_index in range(max_turns):
            run_channel["value"] = "model_turn"
            text = base_prompt if not feedback else f"{base_prompt}\n\n{feedback}"
            scenario_followup = contract.get("behavioral_followup")
            if (turn_index > 0 and isinstance(scenario_followup, str)
                    and scenario_followup):
                text += f"\n\nUSER FOLLOW-UP\n{scenario_followup}"
            started = time.perf_counter()
            turn_action_start = len(action_calls)
            turn_receipt_start = len(cfw_gate_receipts)
            turn_write_request_start = server_request_counts.get(
                "item/fileChange/requestApproval", 0)
            try:
                turn_result = await adapter.run_turn(
                    text, timeout=180 if dynamic_tool_probe else 600)
                turn_records.append({"turn_id": turn_result.get("turn_id"),
                                     "status": turn_result.get("status"),
                                     "error": (dict(turn_result["error"])
                                               if isinstance(turn_result.get("error"), Mapping)
                                               else {}),
                                     "wall_time_ms": round(
                                         (time.perf_counter() - started) * 1000, 3)})
            except asyncio.TimeoutError:
                turn_records.append({"status": "timeout",
                                     "error": {"message": "model turn timeout"},
                                     "wall_time_ms": round(
                                         (time.perf_counter() - started) * 1000, 3)})
                break
            if adapter._behavior_trace:
                turn_trace = adapter._behavior_trace[-1]
                previous_trace = (adapter._behavior_trace[-2]
                                  if len(adapter._behavior_trace) > 1 else None)
                previous_mode = (previous_trace.get("behavioral_mode")
                                 if isinstance(previous_trace, Mapping) else None)
                current_mode = turn_trace.get("behavioral_mode")
                turn_trace.update({
                    "turn_status": str(turn_result.get("status") or "")[:32],
                    "model_tool_call_count": len(action_calls) - turn_action_start,
                    "native_write_request_count": max(
                        0, server_request_counts.get(
                            "item/fileChange/requestApproval", 0)
                        - turn_write_request_start),
                    "cfw_gate_receipt_count": max(
                        0, len(cfw_gate_receipts) - turn_receipt_start),
                    "cfw_gate_decisions": [
                        str(item.get("decision"))[:32]
                        for item in cfw_gate_receipts[turn_receipt_start:]
                        if isinstance(item.get("decision"), str)][:16],
                    "model_validator_order": [
                        str(item.get("validator"))[:96]
                        for item in action_calls[turn_action_start:]
                        if isinstance(item.get("validator"), str)][:32],
                    "mode_transition": (
                        f"{previous_mode}->{current_mode}"[:64]
                        if isinstance(previous_mode, str)
                        and isinstance(current_mode, str) else "initial"),
                    "redirected": current_mode in {"REPLAN", "RECOVER", "REINSPECT"},
                })
            if turn_result.get("status") != "completed":
                break
            if dynamic_tool_probe:
                break
            run_channel["value"] = "host_completion"
            prior_run_count = len(validation_runs)
            prior_completion_attempt_count = len(completion_attempts)
            completion = await adapter.validate_completion()
            record_completion_attempt(completion)
            completion_runs = validation_runs[prior_run_count:]
            if adapter._behavior_trace:
                turn_trace = adapter._behavior_trace[-1]
                completion_validators = completion.get("validators", ())
                turn_trace.update({
                    "completion_decision": str(
                        completion.get("decision") or "")[:64],
                    "completion_attempt_count": (
                        len(completion_attempts) - prior_completion_attempt_count),
                    "completion_validator_order": [
                        str(item.get("validator_id"))[:96]
                        for item in completion_validators
                        if isinstance(item, Mapping)
                        and isinstance(item.get("validator_id"), str)][:32],
                })
            if (completion.get("decision") == "finalization_failed" and with_csw
                    and authority is not None):
                if condition == "triad-multipath":
                    getter_identity = csw_workflow_identities.get("getter")
                    gitter_result = next((item for item in csw_workflow_results
                                          if item.get("statework_id") == "gitter"), {})
                    if getter_identity is None or not getter_prepared:
                        raise RuntimeError(
                            "multi-path completion lacks host-prepared Getter evidence")
                    getter_result = _complete_getter_workflow(
                        authority, getter_identity, workspace, request,
                        adapter._host, prepared=getter_prepared,
                        contract=contract, validator_results=completion_runs,
                        gitter_evidence_ref=str(gitter_result.get("evidence_ref") or ""))
                    csw_workflow_results.append(getter_result)
                    if getter_result.get("completed") is True:
                        retry_run_count = len(validation_runs)
                        completion = await adapter.validate_completion()
                        record_completion_attempt(completion)
                        completion_runs.extend(validation_runs[retry_run_count:])
                else:
                    incomplete = []
                    for statework_id, identity in csw_workflow_identities.items():
                        contract_hash = str(authority.workflow_contract_identity(
                            statework_id)["contract_hash"])
                        snapshot = authority.inspect_workflow(
                            identity, contract_hash=contract_hash)
                        if not snapshot.completed:
                            incomplete.append((statework_id, identity))
                    for statework_id, _identity in incomplete:
                        if statework_id != "gitter":
                            raise RuntimeError(
                                f"host completion requires unsupported StateWork {statework_id!r}")
                    gitter_identity = csw_workflow_identities.get("gitter")
                    if gitter_identity is None:
                        raise RuntimeError("CFW task has no host-owned Gitter workflow identity")
                    csw_workflow_results.append(_complete_gitter_workflow(
                        authority, gitter_identity, workspace, request, adapter._host))
                    retry_run_count = len(validation_runs)
                    completion = await adapter.validate_completion()
                    record_completion_attempt(completion)
                    completion_runs.extend(validation_runs[retry_run_count:])
            completion_results = [dict(item) for item in completion_runs]
            if isinstance(completion.get("validators"), (tuple, list)):
                completion_results = [dict(item) for item in completion["validators"]
                                      if isinstance(item, Mapping)]
            if completion.get("status") == "completed":
                break
            failing = [str(item.get("validator")) for item in completion_runs
                       if item.get("status") != "passed"]
            if not failing:
                failing = [str(item.get("validator_id"))
                           for item in completion.get("validators", ())
                           if not item.get("passed")]
            if turn_index + 1 < max_turns and failing:
                feedback = ("Host validator results after your last turn: failing "
                            "validators: " + ", ".join(failing)
                            + ". Inspect the exact test failures, repair source files, "
                            "then rerun the affected registered validators.")
            else:
                break

        completion_status = (completion.get("status") if "completion" in locals()
                             else "not_checked")
        completion_permitted = (
            True if completion_status == "completed" else
            False if completion_status == "incomplete" else None)
        status = adapter.status()
        model_execution_observed = status.get("model_execution_observed") is True
        failure_class = _execution_failure_class(
            turn_records, model_execution_observed=model_execution_observed)
        model_infrastructure_valid = bool(
            status.get("inference_ready") is True and model_execution_observed)
        final_external = (_run_baseline_validator_set(
            workspace, validation_runs=validation_runs,
            channel="external_final_score")
            if model_execution_observed and not dynamic_tool_probe else [])
        if cognitive_host is not None:
            bridge_status = dict(status.get("cognitive_host_status") or {})
        else:
            bridge_status = {}
        wall_ms = round((time.perf_counter() - wall_started) * 1000, 3)
        expected_runtime = ("combined_enforcing" if with_csw
                            else "standalone_enforcing")
        workflow_states: dict[str, dict[str, Any]] = {}
        if with_csw:
            for statework_id, identity in csw_workflow_identities.items():
                contract_hash = str(authority.workflow_contract_identity(
                    statework_id)["contract_hash"])
                snapshot = authority.inspect_workflow(
                    identity, contract_hash=contract_hash)
                workflow_states[statework_id] = {
                    "state": snapshot.current_state,
                    "completed": bool(snapshot.completed),
                    "revision": int(snapshot.revision),
                }
        csw_selection = (_csw_selection_snapshot(authority, csw_workflow_identities)
                         if with_csw else None)
        invariants = {
            "inference_ready": status.get("inference_ready") is True,
            "model_execution_observed": model_execution_observed,
            "validator_boundary_observed": (
                status.get("validator_activity_observed") is True),
            "completion_checked": status.get("completion_checked") is True,
            "app_server_connected": status.get("connected") is True,
            "no_cognitive_fault": status.get("cognitive_fault") is None,
            "cfw_action_gate_observed": bool(action_calls) and bool(cfw_gate_receipts),
            "model_visibility_verified": status.get("model_visibility_verified") is True,
            "native_mutation_surfaces_closed": (
                status.get("native_mutation_surfaces_closed") is True
                and set(status.get("native_action_features_supported") or ()).issubset(
                    set(status.get("native_action_features_disabled") or ()))),
            "runtime_mode_matches": status.get("runtime_mode") == expected_runtime,
        }
        if with_csw:
            invariants["cfw_workflow_bound"] = bool(workflow_ids)
        if with_dp:
            invariants.update({
                "model_visible_dp_tools_zero": status.get("model_visible_dp_tools") == 0,
                "private_dp_bridge_open": bridge_status.get(
                    "private_dp_bridge_open") is True,
                "cfw_authority_verified": bridge_status.get(
                    "cfw_authority_verified") is True,
                "shared_csw_authority_verified": bridge_status.get(
                    "shared_csw_authority_verified") is True,
                "dp_bridge_fault_free": bridge_status.get("dp_faulted") is False,
                "dp_behavior_revision_advanced": bridge_status.get(
                    "dp_behavior_revision", 0) >= 1,
                "dp_tools_private": bridge_status.get(
                    "shared_csw_workflow_ids") == sorted(workflow_ids),
                "dp_post_action_observed": any(
                    item.get("boundary") == "post_action"
                    for item in dp_observation_records),
            })
        if condition == "triad-multipath":
            invariants.update({
                "multi_workflow_plan_bound": sorted(workflow_ids) == ["getter", "gitter"],
                "multi_workflow_revisions_advanced": all(
                    workflow_states.get(statework_id, {}).get("revision", 0) >= 1
                    for statework_id in ("getter", "gitter")),
            })
        if dynamic_tool_probe:
            probe_observations = {item.get("boundary")
                                  for item in dp_observation_records}
            probe_actions = [item for item in action_calls
                             if item.get("tool") == "cfw_read"]
            invariants.update({
                "probe_cfw_read_observed": bool(probe_actions),
                "probe_cfw_read_succeeded": bool(
                    probe_actions and probe_actions[0].get("success") is True),
                "probe_cfw_preflight_receipt_observed": any(
                    item.get("stage") == "preflight"
                    and item.get("decision") == "allow"
                    for item in cfw_gate_receipts),
                "probe_cfw_final_authorization_observed": any(
                    item.get("stage") == "final"
                    and item.get("decision") == "allow"
                    for item in cfw_gate_receipts),
                "probe_marker_returned": (
                    "CFW_PROBE_OK_2026" in str(adapter._last_assistant_message or "")),
                "probe_pre_action_observed": "pre_action" in probe_observations,
                "probe_post_action_observed": "post_action" in probe_observations,
            })
        return {
            "condition": condition,
            "stack": {"cfw": "cfw-only", "cfw-csw": "cfw-csw",
                      "triad": "cfw-csw-dp",
                      "triad-multipath": "cfw-csw-dp-multipath"}[condition],
            "trial_id": trial_id, "model": model,
            "host_initialized": status.get("cfw_session_id") is not None,
            "cognitive_host_initialized": (
                bridge_status.get("private_dp_bridge_open") is True if with_dp else None),
            "thread_started": status.get("thread_id") is not None,
            "turn_started": bool(turn_records),
            "inference_ready": status.get("inference_ready") is True,
            "inference_readiness": status.get("inference_readiness"),
            "model_execution_observed": model_execution_observed,
            "efficacy_scored": bool(model_execution_observed and final_external),
            "failure_domain": _failure_domain(failure_class),
            "failure_class": failure_class,
            "infrastructure_valid": model_infrastructure_valid,
            "model_infrastructure_valid": model_infrastructure_valid,
            "execution_phase": status.get("execution_phase"),
            "host_integration_valid": all(
                value for key, value in invariants.items()
                if key not in {"inference_ready", "model_execution_observed",
                               "validator_boundary_observed", "completion_checked",
                               "dp_post_action_observed"}
                and not key.startswith("probe_")),
            "turns": turn_records,
            "behavior_trace": status.get("behavior_trace", []),
            "behavior_action_trace": behavior_action_trace[-512:],
            "required_criterion_ids": list(criterion_ids),
            "turn_status": turn_records[-1]["status"] if turn_records else "not_started",
            "assistant_message": str(adapter._last_assistant_message or "")[-4000:],
            "completion_status": completion_status,
            "completion_decision": (completion.get("decision")
                                    if "completion" in locals() else None),
            "completion_error_class": (completion.get("error_class")
                                       if "completion" in locals() else None),
            "completion_error_message": (completion.get("error_message")
                                         if "completion" in locals() else None),
            "completion_permitted": completion_permitted,
            "completion_validators": completion_results,
            "completion_validator_reuse_count": sum(
                1 for item in completion_results
                if isinstance(item, Mapping) and item.get("reused") is True),
            "completion_attempts": completion_attempts,
            "csw_workflow_results": csw_workflow_results,
            "final_validator_results": final_external,
            "overall_wall_time_ms": wall_ms,
            "app_server_thread_id": status.get("thread_id"),
            "app_server_session_id": status.get("app_server_session_id"),
            "runtime_mode": status.get("runtime_mode"),
            "cognitive_fault": status.get("cognitive_fault"),
            "cognitive_fault_detail": status.get("cognitive_fault_detail"),
            "workflow_ids": sorted(workflow_ids),
            "requested_statework_ids": list(requested_statework_ids),
            "workflow_states": workflow_states,
            "csw_selection_snapshot": csw_selection,
            "model_visible_dp_tools": status.get("model_visible_dp_tools"),
            "native_action_features_supported": status.get(
                "native_action_features_supported"),
            "native_action_features_disabled": status.get(
                "native_action_features_disabled"),
            "native_action_feature_categories": status.get(
                "native_action_feature_categories"),
            "native_action_feature_catalog": status.get(
                "native_action_feature_catalog"),
            "cognitive_host_status": bridge_status,
            "cfw_gate_receipts": cfw_gate_receipts,
            "registered_dynamic_tool_names": sorted(adapter.action_bindings),
            "server_request_method_counts": dict(sorted(server_request_counts.items())),
            "integration_invariants": invariants,
            "dp_observation_records": dp_observation_records,
            "app_server_error_events": _app_server_error_events(notifications),
            "integration_valid": bool(turn_records
                and turn_records[-1]["status"] == "completed"
                and all(invariants.values())),
            "dynamic_probe_valid": (bool(turn_records
                and turn_records[-1]["status"] == "completed"
                and all(value for key, value in invariants.items()
                        if key.startswith("probe_"))) if dynamic_tool_probe else None),
            "internal_inference_boundaries_intercepted": False,
        }
    finally:
        await adapter.close()
