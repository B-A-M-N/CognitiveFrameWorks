"""Bounded diagnostic projections for direct Codex trial results."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from codex_app_server_adapter import _safe_error

_APP_SERVER_METHOD = re.compile(r"[A-Za-z0-9_.:/-]{1,96}")


def _record_app_server_notification(notifications: list[dict[str, Any]],
                                   method: str,
                                   params: Mapping[str, Any]) -> None:
    """Keep protocol event shape without retaining model text or tool inputs."""
    if not _APP_SERVER_METHOD.fullmatch(method):
        return
    record: dict[str, Any] = {"method": method}
    if method in {"item/started", "item/completed"}:
        item = params.get("item")
        if isinstance(item, Mapping):
            item_type = item.get("type")
            if isinstance(item_type, str):
                record["item_type"] = item_type[:64]
            status = item.get("status")
            if isinstance(status, str):
                record["item_status"] = status[:32]
            for key in ("toolName", "tool", "name"):
                tool_name = item.get(key)
                if (isinstance(tool_name, str)
                        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", tool_name)):
                    record["tool_name"] = tool_name
                    break
    elif method == "turn/completed":
        turn = params.get("turn")
        if isinstance(turn, Mapping):
            record["turn"] = dict(turn)
    elif method == "error":
        error = params.get("error")
        if isinstance(error, Mapping):
            safe_error = _safe_error(error)
            if safe_error:
                record["error"] = safe_error
            error_kind = error.get("codexErrorInfo")
            if (isinstance(error_kind, str)
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", error_kind)):
                record["error_kind"] = error_kind
        will_retry = params.get("willRetry")
        if type(will_retry) is bool:
            record["will_retry"] = will_retry
    notifications.append(record)

def _app_server_item_counts(
        notifications: list[dict[str, Any]]) -> tuple[dict[str, dict[str, int]],
                                                        dict[str, dict[str, int]]]:
    type_counts: dict[str, dict[str, int]] = {"started": {}, "completed": {}}
    tool_counts: dict[str, dict[str, int]] = {"started": {}, "completed": {}}
    for event in notifications:
        phase = {"item/started": "started", "item/completed": "completed"}.get(
            event.get("method"))
        if phase is None:
            continue
        item_type = event.get("item_type")
        if isinstance(item_type, str):
            type_counts[phase][item_type] = type_counts[phase].get(item_type, 0) + 1
        tool_name = event.get("tool_name")
        if isinstance(tool_name, str):
            tool_counts[phase][tool_name] = tool_counts[phase].get(tool_name, 0) + 1
    return type_counts, tool_counts

def _app_server_error_events(
        notifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the already-sanitized, bounded server error summaries."""
    return [dict(item) for item in notifications if item.get("method") == "error"][:32]


def _validator_channel_metrics(records: Any) -> dict[str, dict[str, int | float]]:
    """Aggregate validator volume and runtime without retaining test output."""
    if not isinstance(records, (tuple, list)):
        return {}
    totals: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        channel = record.get("channel")
        if (not isinstance(channel, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", channel)):
            continue
        metrics = totals.setdefault(channel, {
            "runs": 0, "passed": 0, "failed": 0, "total_elapsed_ms": 0.0,
            "min_elapsed_ms": None, "max_elapsed_ms": None,
        })
        metrics["runs"] += 1
        if record.get("status") == "passed":
            metrics["passed"] += 1
        else:
            metrics["failed"] += 1
        elapsed = record.get("elapsed_ms")
        if (type(elapsed) in {int, float} and math.isfinite(float(elapsed))
                and 0 <= elapsed <= 86_400_000):
            metrics["total_elapsed_ms"] += float(elapsed)
            metrics["min_elapsed_ms"] = (
                elapsed if metrics["min_elapsed_ms"] is None
                else min(metrics["min_elapsed_ms"], elapsed))
            metrics["max_elapsed_ms"] = (
                elapsed if metrics["max_elapsed_ms"] is None
                else max(metrics["max_elapsed_ms"], elapsed))
    result = {}
    for channel, metrics in totals.items():
        runs = metrics["runs"]
        total = round(metrics["total_elapsed_ms"], 3)
        result[channel] = {
            "runs": runs, "passed": metrics["passed"], "failed": metrics["failed"],
            "total_elapsed_ms": total,
            "mean_elapsed_ms": round(total / runs, 3),
            "min_elapsed_ms": metrics["min_elapsed_ms"],
            "max_elapsed_ms": metrics["max_elapsed_ms"],
        }
    return result

def _execution_failure_class(turn_records: list[dict[str, Any]], *,
                             model_execution_observed: bool) -> str | None:
    if model_execution_observed:
        return None
    for turn in reversed(turn_records):
        error = turn.get("error")
        message = (error.get("message", "") if isinstance(error, Mapping) else "")
        normalized = str(message).lower()
        if "workspace routing" in normalized:
            return "workspace_routing_unavailable"
        if "401" in normalized or "unauthorized" in normalized:
            return "provider_authentication_failed"
        if "timeout" in normalized or turn.get("status") == "timeout":
            return "model_backend_timeout"
    return "model_execution_unobserved"


def _failure_domain(failure_class: str | None) -> str | None:
    if failure_class in {"workspace_routing_unavailable",
                         "workspace_routing_discovery_failed",
                         "workspace_routing_malformed"}:
        return "codex_app_server_workspace_routing"
    if failure_class == "provider_authentication_failed":
        return "external_provider_authentication"
    if failure_class:
        return "codex_app_server_inference"
    return None

def _sanitized_trial_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project trial outcomes without retaining model text or tool payloads."""
    def count_map(value: Any, *, pattern: str = r"[A-Za-z0-9_.:-]{1,96}") -> dict[str, int]:
        if not isinstance(value, Mapping):
            return {}
        return {
            key: count for key, count in list(value.items())[:64]
            if isinstance(key, str)
            and re.fullmatch(pattern, key)
            and type(count) is int and 0 <= count <= 1_000_000
        }

    def protocol_count_map(value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            return {}
        return {
            key: count for key, count in list(value.items())[:64]
            if isinstance(key, str)
            and _APP_SERVER_METHOD.fullmatch(key)
            and type(count) is int and 0 <= count <= 1_000_000
        }

    def timer_map(value: Any) -> dict[str, dict[str, int | float]]:
        if not isinstance(value, Mapping):
            return {}
        timers: dict[str, dict[str, int | float]] = {}
        for key, timer in list(value.items())[:64]:
            if (not isinstance(key, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", key)
                    or not isinstance(timer, Mapping)):
                continue
            fields = {}
            for name in ("count", "total_ms", "max_ms"):
                item = timer.get(name)
                if (type(item) in {int, float} and math.isfinite(float(item))
                        and 0 <= item <= 86_400_000):
                    fields[name] = item
            if fields:
                timers[key] = fields
        return timers

    def validator_channel_map(value: Any) -> dict[str, dict[str, int | float]]:
        if not isinstance(value, Mapping):
            return {}
        fields_allowed = {"runs", "passed", "failed", "total_elapsed_ms",
                          "mean_elapsed_ms", "min_elapsed_ms", "max_elapsed_ms"}
        result = {}
        for channel, metrics in list(value.items())[:32]:
            if (not isinstance(channel, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", channel)
                    or not isinstance(metrics, Mapping)):
                continue
            safe = {}
            for key, item in metrics.items():
                if (key in fields_allowed and type(item) in {int, float}
                        and math.isfinite(float(item)) and 0 <= item <= 86_400_000):
                    safe[key] = item
            if safe:
                result[channel] = safe
        return result

    def phase_count_map(value: Any) -> dict[str, dict[str, int]]:
        if not isinstance(value, Mapping):
            return {}
        return {
            phase: count_map(counts)
            for phase, counts in list(value.items())[:2]
            if phase in {"started", "completed"}
        }

    def bool_map(value: Any) -> dict[str, bool | None]:
        if not isinstance(value, Mapping):
            return {}
        return {
            key: item for key, item in list(value.items())[:64]
            if isinstance(key, str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", key)
            and (type(item) is bool or item is None)
        }

    def feature_list(value: Any, limit: int = 128) -> list[str]:
        if not isinstance(value, (tuple, list)):
            return []
        return [feature for feature in value[:limit]
                if isinstance(feature, str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", feature)]

    def feature_category_map(value: Any) -> dict[str, list[str]]:
        if not isinstance(value, Mapping):
            return {}
        allowed = {"model_callable_disabled", "execution_disabled",
                   "persistent_side_effects_disabled",
                   "workspace_dependency_features_disabled",
                   "unclosed_known_host_surfaces",
                   "host_infrastructure_enabled", "infrastructure_enabled"}
        result = {key: feature_list(features)
                  for key, features in value.items() if key in allowed}
        if "host_infrastructure_enabled" in result:
            result["unclosed_known_host_surfaces"] = result.pop(
                "host_infrastructure_enabled")
        if "infrastructure_enabled" in result:
            result["unclosed_known_host_surfaces"] = result.pop(
                "infrastructure_enabled")
        return result

    def safe_strings(value: Any, limit: int = 64) -> list[str]:
        if not isinstance(value, (tuple, list)):
            return []
        return [item for item in value[:limit]
                if isinstance(item, str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", item)]

    def nullable_bool(value: Any) -> bool | None:
        return value if type(value) is bool else None

    behavior_trace: list[dict[str, Any]] = []
    for item in result.get("behavior_trace", ()):
        if not isinstance(item, Mapping):
            continue
        entry: dict[str, Any] = {}
        for key in ("turn_index", "guidance_byte_count"):
            value = item.get(key)
            if type(value) is int and 0 <= value <= 10_000_000:
                entry[key] = value
        digest = item.get("guidance_sha256")
        if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
            entry["guidance_sha256"] = digest
        for key in ("attempt_id", "behavioral_mode", "goal_anchor_state",
                    "retry_recovery_mode", "context_strategy", "response_strategy",
                    "work_mode", "turn_status", "completion_decision",
                    "mode_transition"):
            value = item.get(key)
            if isinstance(value, str):
                entry[key] = value[:128]
        for key in ("active_optional_components", "focus_criterion_ids", "reason_codes",
                    "cfw_gate_decisions", "model_validator_order",
                    "completion_validator_order"):
            entry[key] = safe_strings(item.get(key), limit=32)
        for key in ("model_tool_call_count", "native_write_request_count",
                    "cfw_gate_receipt_count", "completion_attempt_count"):
            value = item.get(key)
            if type(value) is int and 0 <= value <= 1_000_000:
                entry[key] = value
        for key in ("local_yield_event", "global_blocked_event"):
            value = item.get(key)
            if type(value) is bool:
                entry[key] = value
        redirected = item.get("redirected")
        if type(redirected) is bool:
            entry["redirected"] = redirected
        reanchor = item.get("goal_reanchor_required")
        if type(reanchor) is bool:
            entry["goal_reanchor_required"] = reanchor
        goal_hash = item.get("goal_contract_hash")
        if isinstance(goal_hash, str) and re.fullmatch(r"[a-f0-9]{64}", goal_hash):
            entry["goal_contract_hash"] = goal_hash
        counters = item.get("retry_recovery_counters")
        if isinstance(counters, Mapping):
            entry["retry_recovery_counters"] = {
                key: value for key, value in counters.items()
                if key in {"total_actions_since_goal_progress",
                           "support_actions_since_goal_progress",
                           "recovery_epochs_since_goal_progress",
                           "same_approach_retries", "equivalent_retry_count"}
                and type(value) is int and 0 <= value <= 1_000_000}
        behavior_trace.append(entry)

    turns: list[dict[str, Any]] = []
    for item in result.get("turns", ()):
        if not isinstance(item, Mapping):
            continue
        turn: dict[str, Any] = {}
        status = item.get("status")
        if isinstance(status, str):
            turn["status"] = status[:32]
        elapsed = item.get("wall_time_ms")
        if type(elapsed) in {int, float} and 0 <= elapsed <= 3_600_000:
            turn["wall_time_ms"] = elapsed
        safe_error = _safe_error(item.get("error"))
        if safe_error:
            turn["error"] = safe_error
        turns.append(turn)

    errors: list[dict[str, Any]] = []
    for event in result.get("app_server_error_events", ()):
        if not isinstance(event, Mapping):
            continue
        summary: dict[str, Any] = {"method": "error"}
        safe_error = _safe_error(event.get("error"))
        if safe_error:
            summary["error"] = safe_error
        error_kind = event.get("error_kind")
        if isinstance(error_kind, str) and re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,64}", error_kind):
            summary["error_kind"] = error_kind
        will_retry = event.get("will_retry")
        if type(will_retry) is bool:
            summary["will_retry"] = will_retry
        errors.append(summary)

    raw_cognitive = result.get("cognitive_host_status")
    cognitive: dict[str, Any] = {}
    if isinstance(raw_cognitive, Mapping):
        for key in ("status", "private_dp_bridge_open", "cfw_authority_verified",
                    "shared_csw_authority_verified", "dp_faulted"):
            value = raw_cognitive.get(key)
            if key == "status" and isinstance(value, str):
                cognitive[key] = value[:32]
            elif type(value) is bool:
                cognitive[key] = value
        for key in ("runtime_mode",):
            value = raw_cognitive.get(key)
            if isinstance(value, str):
                cognitive[key] = value[:64]
        for key in ("shared_csw_workflow_count", "cfw_behavior_revision",
                    "dp_behavior_revision", "dp_intervention_count"):
            value = raw_cognitive.get(key)
            if type(value) is int and 0 <= value <= 1_000_000:
                cognitive[key] = value
        if "shared_csw_workflow_ids" in raw_cognitive:
            cognitive["shared_csw_workflow_ids"] = safe_strings(
                raw_cognitive.get("shared_csw_workflow_ids"))
        origins = raw_cognitive.get("dp_intervention_creations_by_origin")
        if not isinstance(origins, Mapping):
            origins = raw_cognitive.get("dp_interventions_by_origin")
        if isinstance(origins, Mapping):
            cognitive["dp_intervention_creations_by_origin"] = {
                key: value for key, value in origins.items()
                if key in {"startup", "runtime_behavior", "post_action", "completion"}
                and type(value) is int and 0 <= value <= 1_000_000}
        raw_intervention_metrics = raw_cognitive.get("dp_intervention_metrics")
        if isinstance(raw_intervention_metrics, Mapping):
            intervention_metrics = {
                key: value for key, value in raw_intervention_metrics.items()
                if key in {"unique_created", "active", "applications", "closed",
                           "effective", "ineffective", "unknown"}
                and type(value) is int and 0 <= value <= 1_000_000}
            created_by_origin = raw_intervention_metrics.get("created_by_origin")
            if isinstance(created_by_origin, Mapping):
                intervention_metrics["created_by_origin"] = {
                    key: value for key, value in created_by_origin.items()
                    if key in {"startup", "runtime_behavior", "post_action", "completion"}
                    and type(value) is int and 0 <= value <= 1_000_000}
            semantics = raw_intervention_metrics.get("semantics")
            if isinstance(semantics, str):
                intervention_metrics["semantics"] = semantics[:256]
            cognitive["dp_intervention_metrics"] = intervention_metrics
        for key in ("dp_intervention_count_semantics",
                    "dp_intervention_creations_semantics"):
            value = raw_cognitive.get(key)
            if isinstance(value, str):
                cognitive[key] = value[:256]
        raw_metrics = raw_cognitive.get("production_metrics")
        if isinstance(raw_metrics, Mapping):
            raw_counters = raw_metrics.get("production_counters")
            if isinstance(raw_metrics.get("counters"), Mapping):
                raw_counters = {
                    **(raw_counters if isinstance(raw_counters, Mapping) else {}),
                    **raw_metrics["counters"],
                }
            cognitive["production_metrics"] = {
                "counters": count_map(raw_counters),
                "timers": timer_map(raw_metrics.get("timers")),
            }

    completion_validators: list[dict[str, Any]] = []
    for validator in result.get("completion_validators", ()):
        if not isinstance(validator, Mapping):
            continue
        validator_id = validator.get("validator_id") or validator.get("validator")
        if not isinstance(validator_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,96}", validator_id):
            continue
        completion_validators.append({
            "validator_id": validator_id,
            "passed": validator.get("passed") is True
                or validator.get("status") == "passed",
            "criterion_ids": safe_strings(validator.get("criterion_ids")),
            "reused": validator.get("reused") is True,
            "evidence_ref": (validator.get("evidence_ref")[:256]
                             if isinstance(validator.get("evidence_ref"), str)
                             else ""),
            "result_digest": (validator.get("result_digest")
                              if isinstance(validator.get("result_digest"), str)
                              and re.fullmatch(r"[a-f0-9]{64}",
                                               validator["result_digest"]) else ""),
            "reuse_basis_digest": (validator.get("reuse_basis_digest")
                                   if isinstance(validator.get("reuse_basis_digest"), str)
                                   and re.fullmatch(r"[a-f0-9]{64}",
                                                    validator["reuse_basis_digest"])
                                   else ""),
        })

    completion_attempts: list[dict[str, Any]] = []
    for attempt in result.get("completion_attempts", ()):
        if not isinstance(attempt, Mapping):
            continue
        validators = []
        for validator in attempt.get("validators", ()):
            if not isinstance(validator, Mapping):
                continue
            validator_id = validator.get("validator_id") or validator.get("validator")
            if not isinstance(validator_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,96}", validator_id):
                continue
            validators.append({
                "validator_id": validator_id,
                "passed": validator.get("passed") is True,
                "criterion_ids": safe_strings(validator.get("criterion_ids")),
                "reused": validator.get("reused") is True,
                "evidence_ref": (validator.get("evidence_ref")[:256]
                                 if isinstance(validator.get("evidence_ref"), str)
                                 else ""),
                "result_digest": (validator.get("result_digest")
                                  if isinstance(validator.get("result_digest"), str)
                                  and re.fullmatch(r"[a-f0-9]{64}",
                                                   validator["result_digest"]) else ""),
                "reuse_basis_digest": (validator.get("reuse_basis_digest")
                                       if isinstance(validator.get("reuse_basis_digest"), str)
                                       and re.fullmatch(r"[a-f0-9]{64}",
                                                        validator["reuse_basis_digest"])
                                       else ""),
            })
        completion_attempts.append({
            "attempt": (attempt.get("attempt")
                        if type(attempt.get("attempt")) is int else None),
            "status": (str(attempt.get("status"))[:32]
                       if isinstance(attempt.get("status"), str) else ""),
            "decision": (str(attempt.get("decision"))[:64]
                         if isinstance(attempt.get("decision"), str) else ""),
            "validators": validators[:32],
        })

    cfw_gate_receipts = []
    for receipt in result.get("cfw_gate_receipts", ()):
        if not isinstance(receipt, Mapping):
            continue
        stage = receipt.get("stage")
        decision = receipt.get("decision")
        if (stage not in {"preflight", "final"}
                or not isinstance(decision, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", decision)):
            continue
        cfw_gate_receipts.append({
            "stage": stage,
            "decision": decision,
            "reason_codes": safe_strings(receipt.get("reason_codes"), limit=8),
            "invocation_id": (receipt.get("invocation_id")[:128]
                             if isinstance(receipt.get("invocation_id"), str)
                             and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}",
                                              receipt["invocation_id"]) else ""),
            "action_request_id": (receipt.get("action_request_id")[:128]
                                  if isinstance(receipt.get("action_request_id"), str)
                                  and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}",
                                                   receipt["action_request_id"])
                                  else ""),
            "action_digest": (receipt.get("action_digest")
                              if isinstance(receipt.get("action_digest"), str)
                              and re.fullmatch(r"[a-f0-9]{64}",
                                               receipt["action_digest"]) else ""),
            "cfw_revision": (receipt.get("cfw_revision")
                             if type(receipt.get("cfw_revision")) is int
                             and 0 <= receipt["cfw_revision"] <= 1_000_000 else None),
        })

    behavior_action_trace: list[dict[str, Any]] = []
    for item in result.get("behavior_action_trace", ()):
        if not isinstance(item, Mapping):
            continue
        entry: dict[str, Any] = {}
        for key in ("action_index", "turn_index"):
            value = item.get(key)
            if type(value) is int and 0 <= value <= 1_000_000:
                entry[key] = value
        elapsed_ms = item.get("elapsed_ms")
        if (type(elapsed_ms) in {int, float}
                and 0 <= elapsed_ms <= 86_400_000):
            entry["elapsed_ms"] = round(float(elapsed_ms), 3)
        for key in ("action_class", "result_class", "behavioral_mode",
                    "target_source"):
            value = item.get(key)
            if isinstance(value, str):
                entry[key] = value[:64]
        entry["criterion_ids"] = safe_strings(item.get("criterion_ids"), limit=8)
        entry["focus_criterion_ids"] = safe_strings(
            item.get("focus_criterion_ids"), limit=8)
        target = item.get("target_criterion_id")
        if isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", target):
            entry["target_criterion_id"] = target
        delta = item.get("criterion_progress_delta")
        if type(delta) is int and -256 <= delta <= 256:
            entry["criterion_progress_delta"] = delta
        progress_deltas = item.get("progress_deltas")
        if isinstance(progress_deltas, Mapping):
            entry["progress_deltas"] = {
                key: value for key, value in progress_deltas.items()
                if isinstance(key, str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", key)
                and type(value) is int and -1 <= value <= 1
            }
        for key in ("same_approach_retry_count", "equivalent_retry_count"):
            retries = item.get(key)
            if type(retries) is int and 0 <= retries <= 1_000_000:
                entry[key] = retries
        redirected = item.get("redirected")
        if type(redirected) is bool:
            entry["redirected"] = redirected
        result_class = entry.get("result_class")
        if result_class not in {"success", "failure"}:
            entry["result_class"] = "unknown"
        behavior_action_trace.append(entry)

    model_visible_dp_tools = result.get("model_visible_dp_tools")
    if type(model_visible_dp_tools) is not int:
        boundary_invariants = result.get("boundary_probe_invariants")
        if (isinstance(boundary_invariants, Mapping)
                and boundary_invariants.get("model_visible_dp_tools_zero") is True):
            model_visible_dp_tools = 0

    raw_feature_catalog = result.get("native_action_feature_catalog")
    feature_catalog_summary: dict[str, Any] = {}
    if isinstance(raw_feature_catalog, Mapping):
        for key in ("version", "policy_sha256", "codex_cli_version",
                    "catalog_sha256", "supported_feature_count",
                    "known_closed_count", "disabled_host_feature_count",
                    "feature_catalog_count",
                    "unknown_feature_count", "reviewed_catalog_match",
                    "host_policy_closed"):
            value = raw_feature_catalog.get(key)
            if (isinstance(value, str) or type(value) in {int, bool}):
                feature_catalog_summary[key] = value
        feature_catalog_summary["feature_names"] = feature_list(
            raw_feature_catalog.get("feature_names"), limit=256)

    summary: dict[str, Any] = {
        "schema": "generic-codex-app-server-trial-summary/v1",
        "condition": str(result.get("condition") or "")[:64],
        "trial_id": str(result.get("trial_id") or "")[:128],
        "behavioral_scenario": (
            result.get("behavioral_scenario")
            if isinstance(result.get("behavioral_scenario"), str)
            and re.fullmatch(r"[a-z][a-z0-9-]{1,63}",
                             result["behavioral_scenario"]) else None),
        "required_criterion_ids": safe_strings(
            result.get("required_criterion_ids"), limit=32),
        "model": str(result.get("model") or "")[:128],
        "stack": str(result.get("stack") or "")[:64],
        "turn_status": str(result.get("turn_status") or "")[:32],
        "inference_ready": nullable_bool(result.get("inference_ready")),
        "model_execution_observed": nullable_bool(
            result.get("model_execution_observed")),
        "efficacy_scored": nullable_bool(result.get("efficacy_scored")),
        "infrastructure_valid": nullable_bool(result.get("infrastructure_valid")),
        "model_infrastructure_valid": nullable_bool(result.get(
            "model_infrastructure_valid", result.get("infrastructure_valid"))),
        "failure_domain": str(result.get("failure_domain") or "")[:64],
        "execution_phase": str(result.get("execution_phase") or "")[:64],
        "inference_preflight_only": result.get("inference_preflight_only") is True,
        "host_initialized": result.get("host_initialized") is True,
        "cognitive_host_initialized": nullable_bool(
            result.get("cognitive_host_initialized")),
        "host_integration_valid": nullable_bool(
            result.get("host_integration_valid")),
        "dynamic_probe_valid": nullable_bool(result.get("dynamic_probe_valid")),
        "thread_started": result.get("thread_started") is True,
        "turn_started": result.get("turn_started") is True,
        "workspace_routing_required": result.get("workspace_routing_required") is True,
        "turns": turns[:16],
        "behavior_trace": behavior_trace[:16],
        "behavior_action_trace": behavior_action_trace[:512],
        "assistant_message_present": bool(result.get("assistant_message")),
        "completion_status": str(result.get("completion_status") or "")[:32],
        "completion_decision": (str(result.get("completion_decision"))[:64]
                                if isinstance(result.get("completion_decision"), str)
                                else None),
        "completion_error_class": str(result.get("completion_error_class") or "")[:64],
        "completion_error_message": str(result.get("completion_error_message") or "")[:256],
        "completion_permitted": (
            result.get("completion_permitted")
            if type(result.get("completion_permitted")) is bool else None),
        "criterion_results": bool_map(result.get("criterion_results")),
        "criteria_passed": (result.get("criteria_passed")
                             if type(result.get("criteria_passed")) is int else None),
        "criteria_total": (result.get("criteria_total")
                            if type(result.get("criteria_total")) is int else None),
        "changed_path_count": (len(result.get("changed_paths"))
                                if isinstance(result.get("changed_paths"), list) else None),
        "model_tool_call_count": (result.get("model_tool_call_count")
                                  if type(result.get("model_tool_call_count")) is int else 0),
        "model_validator_call_count": (result.get("model_validator_call_count")
                                       if type(result.get("model_validator_call_count")) is int else 0),
        "server_request_method_counts": protocol_count_map(
            result.get("server_request_method_counts")),
        "app_server_event_counts": protocol_count_map(
            result.get("app_server_event_counts")),
        "app_server_item_type_counts": phase_count_map(
            result.get("app_server_item_type_counts")),
        "app_server_item_tool_counts": phase_count_map(
            result.get("app_server_item_tool_counts")),
        "boundary_probe_valid": nullable_bool(result.get("boundary_probe_valid")),
        "boundary_probe_invariants": bool_map(
            result.get("boundary_probe_invariants")),
        "dp_observation_record_count": (len(result.get("dp_observation_records"))
                                         if isinstance(result.get("dp_observation_records"), list)
                                         else 0),
        "cfw_gate_receipt_count": (len(result.get("cfw_gate_receipts"))
                                   if isinstance(result.get("cfw_gate_receipts"), list)
                                   else 0),
        "cfw_gate_receipts": cfw_gate_receipts[:128],
        "cfw_block_count": (result.get("cfw_block_count")
                            if type(result.get("cfw_block_count")) is int else 0),
        "model_visible_dp_tools": (model_visible_dp_tools
                                   if type(model_visible_dp_tools) is int else None),
        "workflow_ids": safe_strings(result.get("workflow_ids")),
        "requested_statework_ids": safe_strings(result.get("requested_statework_ids")),
        "registered_dynamic_tool_names": safe_strings(
            result.get("registered_dynamic_tool_names")),
        "completion_validators": completion_validators[:32],
        "completion_validator_reuse_count": (
            result.get("completion_validator_reuse_count")
            if type(result.get("completion_validator_reuse_count")) is int
            and 0 <= result["completion_validator_reuse_count"] <= 64 else 0),
        "completion_attempts": completion_attempts[:16],
        "validator_channel_metrics": validator_channel_map(
            result.get("validator_channel_metrics")),
        "app_server_error_event_count": len(errors),
        "app_server_error_events": errors[:32],
        "overall_wall_time_ms": (
            result.get("overall_wall_time_ms")
            if type(result.get("overall_wall_time_ms")) in {int, float}
            and 0 <= result["overall_wall_time_ms"] <= 86_400_000 else None),
        "runtime_mode": str(result.get("runtime_mode") or "")[:64],
        "cognitive_fault": str(result.get("cognitive_fault") or "")[:128],
        "native_action_features_supported": feature_list(
            result.get("native_action_features_supported")),
        "native_action_features_disabled": feature_list(
            result.get("native_action_features_disabled")),
        "native_action_feature_categories": feature_category_map(
            result.get("native_action_feature_categories")),
        "native_action_feature_catalog": feature_catalog_summary,
        "integration_invariants": bool_map(result.get("integration_invariants")),
        "integration_valid": nullable_bool(result.get("integration_valid")),
        "internal_inference_boundaries_intercepted": (
            result.get("internal_inference_boundaries_intercepted") is True),
    }
    fixture_state = result.get("fixture_state_after_failed_start")
    if cognitive:
        summary["cognitive_host_status"] = cognitive
    if isinstance(fixture_state, Mapping):
        summary["fixture_state_after_failed_start"] = {
            "source_sha256": (fixture_state.get("source_sha256")
                              if isinstance(fixture_state.get("source_sha256"), str)
                              and re.fullmatch(r"[a-f0-9]{64}",
                                               fixture_state["source_sha256"])
                              else None),
            "changed_path_count": (fixture_state.get("changed_path_count")
                                   if type(fixture_state.get("changed_path_count")) is int
                                   and 0 <= fixture_state["changed_path_count"] <= 100_000
                                   else None),
            "validator_results": [
                {"validator": item["validator"], "status": item["status"]}
                for item in fixture_state.get("validator_results", ())
                if isinstance(item, Mapping)
                and isinstance(item.get("validator"), str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", item["validator"])
                and item.get("status") in {"passed", "failed"}
            ][:32],
        }
    readiness = result.get("inference_readiness")
    if isinstance(readiness, Mapping):
        summary["inference_readiness"] = {
            key: value for key, value in readiness.items()
            if key in {"ready", "auth_mode", "workspace_routing_required",
                       "workspace_routing_ready", "provider_id", "account_type",
                       "requirements_loaded", "failure_class"}
            and (value is None or type(value) in {str, bool})
        }
    failure_class = result.get("failure_class")
    if isinstance(failure_class, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", failure_class):
        summary["failure_class"] = failure_class
    failure_message = _safe_error({"message": result.get("failure_message")})
    if failure_message.get("message"):
        summary["failure_message"] = failure_message["message"]
    return summary
