from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integration_tests"))

from codex_eval import launch as codex_launch
from codex_eval.report import (
    _failure_domain,
    _validator_channel_metrics,
)
from codex_host import launch as host_launch
from codex_host.diagnostics import (
    _native_feature_catalog,
    _native_feature_categories,
)
from codex_host.launch import CodexLaunchConfigurationError
from live_codex_app_server_coding_trial import (
    _app_server_error_events,
    _app_server_item_counts,
    _record_app_server_notification,
    _sanitized_trial_summary,
)
from run_codex_app_server_coding_evaluation import _summarize


def test_notification_capture_records_tool_metadata_without_payloads():
    events = []
    _record_app_server_notification(events, "item/started", {
        "item": {
            "id": "private-item-id",
            "type": "dynamicToolCall",
            "name": "cfw_read",
            "arguments": {"path": "/private/workspace/file"},
            "text": "private model output",
            "status": "inProgress",
        },
    })
    _record_app_server_notification(events, "item/completed", {
        "item": {
            "id": "private-item-id",
            "type": "dynamicToolCall",
            "name": "cfw_read",
            "arguments": {"path": "/private/workspace/file"},
            "status": "completed",
        },
    })

    assert events == [
        {"method": "item/started", "item_type": "dynamicToolCall",
         "item_status": "inProgress", "tool_name": "cfw_read"},
        {"method": "item/completed", "item_type": "dynamicToolCall",
         "item_status": "completed", "tool_name": "cfw_read"},
    ]
    assert _app_server_item_counts(events) == (
        {"started": {"dynamicToolCall": 1},
         "completed": {"dynamicToolCall": 1}},
        {"started": {"cfw_read": 1},
         "completed": {"cfw_read": 1}},
    )


def test_sanitized_report_preserves_slash_separated_app_server_methods():
    events = []
    for method in ("item/tool/call", "turn/started", "turn/completed"):
        _record_app_server_notification(events, method, {})

    summary = _sanitized_trial_summary({
        "condition": "triad", "trial_id": "protocol-method-test",
        "server_request_method_counts": {"item/tool/call": 1},
        "app_server_event_counts": {"turn/started": 1, "turn/completed": 1},
    })

    assert [event["method"] for event in events] == [
        "item/tool/call", "turn/started", "turn/completed"]
    assert summary["server_request_method_counts"] == {"item/tool/call": 1}
    assert summary["app_server_event_counts"] == {
        "turn/started": 1, "turn/completed": 1}


def test_error_notification_capture_keeps_only_sanitized_diagnostics():
    events = []
    _record_app_server_notification(events, "error", {
        "threadId": "private-thread-id",
        "turnId": "private-turn-id",
        "error": {
            "code": -32603,
            "message": "workspace routing discovery failed token=secret-value",
            "codexErrorInfo": "other",
            "additionalDetails": {"token": "secret-value"},
        },
        "willRetry": False,
    })

    expected = {
        "method": "error",
        "error": {
            "code": -32603,
            "message": "workspace routing discovery failed token=[redacted]",
        },
        "error_kind": "other",
        "will_retry": False,
    }
    assert events == [expected]
    assert _app_server_error_events(events) == [expected]
    assert "secret-value" not in repr(events)
    assert "private-thread-id" not in repr(events)


def test_sanitized_trial_summary_preserves_errors_without_model_or_tool_payloads():
    summary = _sanitized_trial_summary({
        "condition": "triad-multipath",
        "trial_id": "summary-test",
        "model": "gpt-6-luna",
        "turn_status": "failed",
        "assistant_message": "private assistant text",
        "turns": [{
            "status": "failed", "wall_time_ms": 12.5,
            "error": {"code": -32603,
                      "message": "workspace routing discovery failed token=secret"},
        }],
        "behavior_trace": [{
            "turn_index": 1, "guidance_byte_count": 42,
            "guidance_sha256": "a" * 64, "behavioral_mode": "REPLAN",
            "mode_transition": "initial", "turn_status": "completed",
            "completion_decision": "validator_failed",
            "focus_criterion_ids": ["parser_fix"], "redirected": True,
            "model_tool_call_count": 3, "native_write_request_count": 0,
            "cfw_gate_receipt_count": 2,
            "cfw_gate_decisions": ["allow", "block"],
            "model_validator_order": ["parser"],
            "completion_validator_order": ["parser"],
            "retry_recovery_counters": {"same_approach_retries": 1},
            "private_workspace_path": "/should/not/appear",
        }],
        "behavior_action_trace": [{
            "action_index": 1, "turn_index": 1, "elapsed_ms": 12.5,
            "target_criterion_id": "parser_fix", "target_source": "test_path",
            "criterion_ids": ["parser_fix"], "action_class": "read",
            "result_class": "success", "same_approach_retry_count": 0,
            "equivalent_retry_count": 0,
        }],
        "required_criterion_ids": ["parser_fix"],
        "action_calls": [{"arguments": {"path": "/private/path"}}],
        "app_server_error_events": [{
            "method": "error",
            "error": {"code": -32603,
                      "message": "workspace routing discovery failed token=secret"},
            "error_kind": "other", "will_retry": False,
            "params": {"private": "payload"},
        }],
        "app_server_item_type_counts": {
            "started": {"userMessage": 1}, "completed": {"userMessage": 1}},
        "app_server_item_tool_counts": {"started": {}, "completed": {}},
        "boundary_probe_valid": True,
        "boundary_probe_invariants": {
            "app_server_connected": True, "completion_not_applicable": None},
        "integration_invariants": {"app_server_connected": True},
        "cognitive_host_status": {
            "status": "ready", "private_dp_bridge_open": True,
            "cfw_authority_verified": True, "shared_csw_authority_verified": True,
            "dp_faulted": False, "dp_behavior_revision": 7,
            "dp_intervention_count": 3, "shared_csw_workflow_ids": ["getter", "gitter"],
            "dp_intervention_metrics": {
                "unique_created": 3, "active": 1, "applications": 3,
                "closed": 2, "effective": 1, "ineffective": 0,
                "unknown": 1,
                "created_by_origin": {
                    "startup": 1, "runtime_behavior": 1,
                    "post_action": 1, "completion": 0},
                "semantics": "bounded intervention accounting",
            },
            "production_metrics": {
                "counters": {"control_ticks_committed": 4},
                "production_counters": {"reconciliations": 17},
                "timers": {"control_tick": {
                    "count": 2, "total_ms": 13.5, "max_ms": 8.0}},
            },
            "dp_fault_reason": "private fault detail",
        },
        "dp_observation_records": [{"private": "payload"}],
        "cfw_gate_receipts": [{
            "stage": "final", "decision": "allow",
            "reason_codes": ["host.gate_allowed"],
            "invocation_id": "call-17",
            "action_request_id": "action-request-17",
            "action_digest": "c" * 64, "cfw_revision": 9,
            "private": "receipt"}],
        "cfw_block_count": 1,
        "model_visible_dp_tools": 0,
        "workflow_ids": ["getter", "gitter"],
        "requested_statework_ids": ["getter"],
        "registered_dynamic_tool_names": ["cfw_read"],
        "completion_validators": [{
            "validator": "parser", "status": "passed", "criterion_ids": ["parser_fix"],
            "evidence_ref": "ev:validator:1", "result_digest": "a" * 64,
            "reuse_basis_digest": "b" * 64,
            "private": "result payload",
        }],
        "completion_permitted": None,
        "completion_attempts": [{
            "attempt": 1, "status": "incomplete", "decision": "validator_failed",
            "validators": [{"validator_id": "coding-parser-v1",
                            "criterion_ids": ["parser_fix"], "passed": False,
                            "evidence_ref": "ev:validator:2",
                            "result_digest": "c" * 64,
                            "reuse_basis_digest": "d" * 64}],
        }],
        "server_request_method_counts": {"item/tool/call": 3, "thread/start": 1},
        "app_server_event_counts": {
            "turn/started": 1, "turn/completed": 1, "item/completed": 2},
        "validator_channel_metrics": {
            "host_completion": {
                "runs": 2, "passed": 1, "failed": 1,
                "total_elapsed_ms": 120.5, "mean_elapsed_ms": 60.25,
                "min_elapsed_ms": 45.0, "max_elapsed_ms": 75.5,
                "private": "dropped",
            },
        },
    })

    assert summary["turns"][0]["error"] == {
        "code": -32603,
        "message": "workspace routing discovery failed token=[redacted]",
    }
    assert summary["app_server_error_events"] == [{
        "method": "error",
        "error": {"code": -32603,
                  "message": "workspace routing discovery failed token=[redacted]"},
        "error_kind": "other", "will_retry": False,
    }]
    assert summary["assistant_message_present"] is True
    behavior_trace = summary["behavior_trace"][0]
    expected_trace = {
        "turn_index": 1, "guidance_byte_count": 42,
        "guidance_sha256": "a" * 64, "behavioral_mode": "REPLAN",
        "mode_transition": "initial", "turn_status": "completed",
        "completion_decision": "validator_failed",
        "focus_criterion_ids": ["parser_fix"], "redirected": True,
        "model_tool_call_count": 3, "native_write_request_count": 0,
        "cfw_gate_receipt_count": 2,
        "cfw_gate_decisions": ["allow", "block"],
        "model_validator_order": ["parser"],
        "completion_validator_order": ["parser"],
        "retry_recovery_counters": {"same_approach_retries": 1},
    }
    assert all(behavior_trace.get(key) == value
               for key, value in expected_trace.items())
    assert "private_workspace_path" not in behavior_trace
    assert summary["required_criterion_ids"] == ["parser_fix"]
    assert summary["behavior_action_trace"] == [{
        "action_index": 1, "turn_index": 1, "elapsed_ms": 12.5,
        "target_criterion_id": "parser_fix", "target_source": "test_path",
        "criterion_ids": ["parser_fix"], "focus_criterion_ids": [],
        "action_class": "read", "result_class": "success",
        "same_approach_retry_count": 0, "equivalent_retry_count": 0,
    }]
    assert summary["boundary_probe_valid"] is True
    assert summary["boundary_probe_invariants"] == {
        "app_server_connected": True, "completion_not_applicable": None}
    assert summary["cognitive_host_status"]["dp_behavior_revision"] == 7
    assert summary["cognitive_host_status"]["dp_intervention_metrics"] == {
        "unique_created": 3, "active": 1, "applications": 3,
        "closed": 2, "effective": 1, "ineffective": 0,
        "unknown": 1,
        "created_by_origin": {
            "startup": 1, "runtime_behavior": 1,
            "post_action": 1, "completion": 0},
        "semantics": "bounded intervention accounting",
    }
    assert summary["cognitive_host_status"]["production_metrics"] == {
        "counters": {"control_ticks_committed": 4, "reconciliations": 17},
        "timers": {"control_tick": {
            "count": 2, "total_ms": 13.5, "max_ms": 8.0}}}
    assert summary["dp_observation_record_count"] == 1
    assert summary["cfw_gate_receipt_count"] == 1
    assert summary["cfw_gate_receipts"] == [{
        "stage": "final", "decision": "allow",
        "reason_codes": ["host.gate_allowed"],
        "invocation_id": "call-17", "action_request_id": "action-request-17",
        "action_digest": "c" * 64, "cfw_revision": 9}]
    assert summary["completion_validators"] == [{
        "validator_id": "parser", "passed": True, "criterion_ids": ["parser_fix"],
        "reused": False, "evidence_ref": "ev:validator:1",
        "result_digest": "a" * 64, "reuse_basis_digest": "b" * 64}]
    assert summary["completion_permitted"] is None
    assert summary["completion_attempts"] == [{
        "attempt": 1, "status": "incomplete", "decision": "validator_failed",
        "validators": [{"validator_id": "coding-parser-v1", "passed": False,
                        "criterion_ids": ["parser_fix"], "reused": False,
                        "evidence_ref": "ev:validator:2",
                        "result_digest": "c" * 64,
                        "reuse_basis_digest": "d" * 64}],
    }]
    assert summary["server_request_method_counts"] == {
        "item/tool/call": 3, "thread/start": 1}
    assert summary["app_server_event_counts"] == {
        "turn/started": 1, "turn/completed": 1, "item/completed": 2}
    assert summary["validator_channel_metrics"] == {
        "host_completion": {
            "runs": 2, "passed": 1, "failed": 1,
            "total_elapsed_ms": 120.5, "mean_elapsed_ms": 60.25,
            "min_elapsed_ms": 45.0, "max_elapsed_ms": 75.5}}
    assert "assistant_message" not in summary
    assert "action_calls" not in summary
    assert "secret" not in repr(summary)
    assert "/private/path" not in repr(summary)
    assert "private assistant text" not in repr(summary)
    assert "private fault detail" not in repr(summary)
    assert "result payload" not in repr(summary)


def test_sanitized_summary_derives_zero_dp_tools_from_boundary_invariant():
    summary = _sanitized_trial_summary({
        "boundary_probe_invariants": {"model_visible_dp_tools_zero": True},
    })

    assert summary["model_visible_dp_tools"] == 0


def test_sanitizer_keeps_absent_hosts_and_probes_not_applicable():
    summary = _sanitized_trial_summary({
        "condition": "cfw", "cognitive_host_status": {},
        "cognitive_host_initialized": None, "dynamic_probe_valid": None,
        "boundary_probe_valid": None, "integration_valid": None,
        "completion_decision": None, "completion_permitted": None,
        "inference_ready": None, "model_execution_observed": None,
        "efficacy_scored": None, "infrastructure_valid": None,
        "model_infrastructure_valid": None, "host_integration_valid": None,
    })

    assert summary["cognitive_host_initialized"] is None
    assert summary["dynamic_probe_valid"] is None
    assert summary["boundary_probe_valid"] is None
    assert summary["integration_valid"] is None
    assert summary["completion_decision"] is None
    assert summary["completion_permitted"] is None
    assert summary["inference_ready"] is None
    assert summary["model_execution_observed"] is None
    assert summary["efficacy_scored"] is None
    assert summary["infrastructure_valid"] is None
    assert summary["model_infrastructure_valid"] is None
    assert summary["host_integration_valid"] is None
    assert "cognitive_host_status" not in summary


def test_sanitizer_preserves_explicit_inference_status_values():
    for value in (None, True, False):
        summary = _sanitized_trial_summary({
            "inference_ready": value,
            "model_execution_observed": value,
            "efficacy_scored": value,
            "infrastructure_valid": value,
            "model_infrastructure_valid": value,
            "host_integration_valid": value,
        })

        assert summary["inference_ready"] is value
        assert summary["model_execution_observed"] is value
        assert summary["efficacy_scored"] is value
        assert summary["infrastructure_valid"] is value
        assert summary["model_infrastructure_valid"] is value
        assert summary["host_integration_valid"] is value


def test_model_unrun_summary_keeps_fixture_state_out_of_efficacy_scores():
    summary = _sanitized_trial_summary({
        "condition": "triad-multipath",
        "trial_id": "routing-blocked",
        "inference_ready": False,
        "model_execution_observed": False,
        "efficacy_scored": False,
        "infrastructure_valid": False,
        "model_infrastructure_valid": False,
        "failure_domain": _failure_domain("workspace_routing_unavailable"),
        "failure_class": "workspace_routing_unavailable",
        "criteria_passed": None,
        "criteria_total": None,
        "fixture_state_after_failed_start": {
            "source_sha256": "a" * 64,
            "changed_path_count": 0,
            "validator_results": [{"validator": "parser", "status": "failed"}],
            "private_fixture_text": "must not leak",
        },
    })

    assert summary["failure_domain"] == "codex_app_server_workspace_routing"
    assert summary["model_infrastructure_valid"] is False
    assert summary["efficacy_scored"] is False
    assert summary["criteria_passed"] is None
    assert summary["criteria_total"] is None
    assert summary["fixture_state_after_failed_start"] == {
        "source_sha256": "a" * 64,
        "changed_path_count": 0,
        "validator_results": [{"validator": "parser", "status": "failed"}],
    }


def test_evaluation_aggregates_sanitized_dp_counters_and_keeps_baseline_completion_na():
    rows = [
        {"condition": "baseline", "efficacy_valid": True,
         "completion_permitted": None},
        {"condition": "triad", "efficacy_valid": True,
         "completion_permitted": True,
         "criteria_passed": 4, "overall_wall_time_ms": 1500,
         "model_tool_call_count": 6, "model_validator_call_count": 2,
         "validator_channel_metrics": {"host_completion": {
             "runs": 2, "passed": 2, "failed": 0, "total_elapsed_ms": 30.0}},
         "cognitive_host_status": {
             "dp_intervention_count": 3,
             "production_metrics": {"counters": {
                 "control_ticks_committed": 7, "reconciliations": 2}},
         }},
    ]

    summary = _summarize(rows, [], ("baseline", "triad"))["conditions"]

    assert summary["baseline"]["completion_applicable_trials"] == 0
    assert summary["baseline"]["completion_permits"] is None
    assert summary["baseline"]["completion_denials_or_incomplete"] is None
    assert summary["triad"]["dp_control_ticks_committed"] == 7
    assert summary["triad"]["dp_reconciliations"] == 2
    assert summary["triad"]["wall_time_ms_distribution"]["median"] == 1500
    assert summary["triad"]["model_tool_calls_distribution"]["max"] == 6
    assert summary["triad"]["validator_channel_metrics"]["host_completion"][
        "total_elapsed_ms"] == 30
    assert _validator_channel_metrics([
        {"channel": "host_completion", "status": "passed", "elapsed_ms": 12},
        {"channel": "host_completion", "status": "failed", "elapsed_ms": 8},
    ]) == {"host_completion": {
        "runs": 2, "passed": 1, "failed": 1, "total_elapsed_ms": 20.0,
        "mean_elapsed_ms": 10.0, "min_elapsed_ms": 8, "max_elapsed_ms": 12}}


def test_external_provider_credential_source_order_has_no_machine_path_default(
        tmp_path, monkeypatch):
    key_name = "FREEINFERENCE_API_KEY"
    cli_file = tmp_path / "cli.env"
    env_file = tmp_path / "environment.env"
    cli_file.write_text(f"{key_name}=from-cli-file\n", encoding="utf-8")
    env_file.write_text(f"{key_name}=from-env-file\n", encoding="utf-8")
    cli_file.chmod(0o600)
    env_file.chmod(0o600)
    monkeypatch.setattr(codex_launch, "_direct_codex_binary", lambda: "/packaged/codex")
    monkeypatch.setenv(key_name, "from-direct-environment")
    args = SimpleNamespace(
        auth_mode="external-provider", provider_env_key=key_name,
        provider_env_file=cli_file, provider_id="freeinference",
        provider_base_url="https://freeinference.org/v1",
        provider_wire_api="responses")

    _, selected = codex_launch._launch_profile(args, tmp_path / "codex-home")
    assert selected[key_name] == "from-cli-file"

    args.provider_env_file = None
    monkeypatch.setenv("CODEX_PROVIDER_ENV_FILE", str(env_file))
    _, selected = codex_launch._launch_profile(args, tmp_path / "codex-home")
    assert selected[key_name] == "from-env-file"

    monkeypatch.delenv("CODEX_PROVIDER_ENV_FILE")
    _, selected = codex_launch._launch_profile(args, tmp_path / "codex-home")
    assert selected[key_name] == "from-direct-environment"

    monkeypatch.delenv(key_name)
    with pytest.raises(CodexLaunchConfigurationError, match="provider credential is unavailable"):
        codex_launch._launch_profile(args, tmp_path / "codex-home")


def test_native_feature_policy_is_version_pinned_and_unknown_catalog_fails_closed(
        monkeypatch):
    categories = _native_feature_categories(
        ["shell_tool", "host_service"], ["shell_tool"])
    identity = _native_feature_catalog(["shell_tool"], ["shell_tool"])
    catalog = _native_feature_catalog(
        ["shell_tool"], ["shell_tool"], {"feature_names": ["shell_tool"]})

    assert categories["unclosed_known_host_surfaces"] == []
    assert catalog["feature_catalog_count"] == 1
    assert catalog["known_closed_count"] == 1
    with pytest.raises(RuntimeError, match="Codex CLI feature catalog is unknown"):
        host_launch._native_feature_catalog_identity(
            ["unreviewed_feature"], host_launch.REVIEWED_CODEX_CLI_VERSION)
    with pytest.raises(RuntimeError, match="Codex CLI feature catalog is unknown"):
        host_launch._native_feature_catalog_identity(
            ["shell_tool"], "codex-cli 0.160.1")
    assert "infrastructure_enabled" not in categories
    assert identity["version"] == "codex-native-host-policy-v1"
    assert identity["policy_sha256"] == (
        "82b363a94a1372a9ef42cafe5b160c67f38bf2502583d34858faa97d8b4bada2")
    monkeypatch.setattr(host_launch, "_NATIVE_EXEC_FEATURES", ("new_exec_surface",))
    with pytest.raises(RuntimeError, match="native feature policy catalog is unknown"):
        _native_feature_catalog(["new_exec_surface"], [])
