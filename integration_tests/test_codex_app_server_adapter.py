from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cognitive_runtime_core.actions import HostActionBinding
from cognitive_runtime_core.harness import (
    CallbackCognitiveHarnessLifecycle,
    HarnessBoundary,
)
from cognitive_runtime_core.validators import HostValidatorBinding

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codex_app_server_adapter as codex_adapter_api
from codex_app_server_adapter import (
    CodexActionBinding,
    CodexAppServerAdapter,
    CodexValidatorBinding,
    JsonlAppServerClient,
    _host_only_feature_args,
)
from codex_app_server_dp_bridge import DigitalPsychologyBridgeHost
from cognitive_runtime_core.integrations.cfw import DigitalPsychologyHarnessLifecycle
from codex_host.launch import CodexLaunchProfile
from codex_host.readiness import CodexInfrastructureUnavailable
from digital_psychology_adapter import CFWBehaviorAdapter
from runtime_contract.actions import (
    ActionDescriptor,
    default_action_registry,
)
from runtime_contract.task import (
    AcceptanceCriterion,
    TaskGoalContract,
    TaskRequest,
    VerificationPolicy,
)
from runtime_contract.workflow_port import (
    WorkflowEligibility,
    WorkflowResolution,
)
from runtime_host.host_api import CognitiveRuntime as CFWRuntime
from runtime_session.lifecycle import GateDecision


def test_codex_adapter_protects_its_skill_registry(tmp_path):
    codex_home = tmp_path / "codex-home"
    context = CodexAppServerAdapter._codex_host_context(
        None,
        CodexLaunchProfile(
            binary="/packaged/codex", auth_mode="chatgpt",
            codex_home=codex_home),
    )
    target = codex_home / "skills" / "cognitiveframeworks_runtime" / "runtime.py"

    assert context.protected_resources.classify_write({"target": str(target)}) == "protected"


class _Host:
    def __init__(self, decision=GateDecision.ALLOW):
        self.decision = decision
        self.prepared = []
        self.results = []
        self.failures = []
        self.attempts = []
        self.observations = []
        self.validator_evidence = []

    def prepare_tool(self, action):
        self.prepared.append(action)
        return SimpleNamespace(
            decision=self.decision,
            reason_codes=("test_gate",),
            action_request_id=("action-1" if self.decision == GateDecision.ALLOW else None),
        )

    def after_tool(self, **kwargs):
        self.results.append(kwargs)
        return "tool-evidence-1"

    def complete_action(self):
        return None

    def observation(self, *args, **kwargs):
        self.observations.append((args, kwargs))
        return "observation-1"

    def begin_attempt(self, **kwargs):
        self.attempts.append(kwargs)

    def record_attempt_failure(self, **kwargs):
        self.failures.append(kwargs)

    def validate_completion_boundary(self, **kwargs):
        return GateDecision.ALLOW

    def finish_task(self, output):
        return {"completed": True}

    def after_validator(self, **kwargs):
        self.validator_evidence.append(kwargs)
        return "validator-evidence-1"


class _TaskHandle:
    def __init__(self, session_id="cfw-session-1"):
        self.session_id = session_id
        self.closed = False
        self.model_calls = 0

    def before_model_call(self, *, tool_capable=False):
        self.model_calls += 1
        return "runtime guidance"

    def base_model_context(self):
        return "base runtime guidance"

    def close(self):
        self.closed = True


class _Runtime:
    def __init__(self, host=None, events=None):
        self.action_registry = default_action_registry()
        self.host = host or _Host()
        self.starts = []
        self.events = events if events is not None else []

    def begin_task(self, request, **kwargs):
        self.starts.append((request, kwargs))
        self.events.append("cfw_task_start")
        return _TaskHandle(kwargs.get("session_id", "cfw-session-1"))

    def host_handle(self, task_handle):
        return self.host


class _EmptyWorkflowPort:
    def snapshot(self, **kwargs):
        return {"revision": 0}

    def active_guidance(self, **kwargs):
        return {"revision": 0, "guidance": []}

    def apply_step(self, **kwargs):
        return {"accepted": False}

    def eligible_routes(self, **kwargs):
        return WorkflowEligibility(eligible_routes=())

    def resolve_workflows(self, **kwargs):
        return WorkflowResolution(
            execution_order=(), active_workflows=(), capsules={},
            component_requirements=(), component_enhancements=(),
            registry_version="", registry_hash="", dependency_checks_passed=True)

    def rank_legal_routes(self, **kwargs):
        raise AssertionError("empty workflow port has no routes to rank")

    def validate_route_ranking(self, **kwargs):
        return False


class _Client:
    # The fake transport models the adapter's controlled launcher: no MCP or
    # native mutation surfaces are attached to the model thread.
    host_policy_verified = True

    def __init__(self, *, thread_id="thread-1", session_id="session-1",
                 early_turn=None, events=None):
        self.codex_feature_catalog = {
            "codex_cli_version": "codex-cli 0.160.0",
            "feature_names": ["dynamic_tools"],
        }
        self.thread_id = thread_id
        self.session_id = session_id
        self.early_turn = early_turn
        self.handlers = None
        self.closed = False
        self.turn_start_params = None
        self.events = events if events is not None else []

    def set_handlers(self, *, request_handler, notification_handler):
        self.handlers = (request_handler, notification_handler)

    async def request(self, method, params=None, *, timeout=30.0):
        self.events.append(method)
        if method == "initialize":
            return {}
        if method == "account/read":
            return {"account": {"type": "apiKey"}, "requiresOpenaiAuth": False}
        if method == "configRequirements/read":
            return {"requirements": None}
        if method in {"thread/resume", "thread/start"}:
            thread = {"id": self.thread_id}
            if self.session_id is not None:
                thread["sessionId"] = self.session_id
            return {"thread": thread}
        if method == "turn/start":
            self.turn_start_params = params
            if self.early_turn is not None:
                self.handlers[1]("turn/completed", {"turn": self.early_turn})
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(f"unexpected App Server request: {method}")

    async def notify(self, method, params=None):
        return None

    async def close(self):
        self.closed = True


def test_workspace_routing_preflight_fails_before_cfw_or_dp_startup(tmp_path):
    class _RoutingFailureClient(_Client):
        async def request(self, method, params=None, *, timeout=30.0):
            self.events.append(method)
            if method == "initialize":
                return {}
            if method == "account/read":
                raise RuntimeError("workspace routing discovery failed")
            raise AssertionError(f"cognitive startup happened before readiness: {method}")

    async def run():
        events = []
        runtime = _Runtime(events=events)
        lifecycle_events = []
        lifecycle = CallbackCognitiveHarnessLifecycle(
            lambda event: lifecycle_events.append(event) or {"status": "ready"})
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: {"ok": True}),),
            model="test-model", cwd=tmp_path, runtime=runtime,
            runtime_harness_lifecycle=lifecycle,
            launch_profile=CodexLaunchProfile(
                binary="/packaged/codex", auth_mode="chatgpt",
                codex_home=tmp_path / "temporary-codex-home"))
        client = _RoutingFailureClient(events=events)
        with pytest.raises(CodexInfrastructureUnavailable) as captured:
            await adapter.open(client)
        assert captured.value.kind == "workspace_routing_unavailable"
        assert runtime.starts == []
        assert lifecycle_events == []
        assert "thread/start" not in events
        assert client.closed is True

    asyncio.run(run())


def test_codex_exposes_only_the_neutral_runtime_lifecycle():
    parameters = inspect.signature(CodexAppServerAdapter).parameters
    assert "runtime_harness_lifecycle" in parameters
    assert "cognitive_host" not in parameters
    assert not hasattr(codex_adapter_api, "CodexCognitiveHost")


def _request():
    return TaskRequest(task_id="task-1", application_id="test-app",
                       goal_contract=TaskGoalContract(
                           objective="Read the requested document",
                           deliverable_kind="analysis", work_mode="research",
                       acceptance_criteria=(AcceptanceCriterion(
                           "criterion-1", "Document was read", "repository_evidence"),)))


def test_host_only_launcher_disables_native_execution_and_external_tools():
    native_surfaces = {
        "shell_tool", "unified_exec", "unified_exec_tty", "unified_exec_zsh_fork",
        "shell_zsh_fork",
        "code_mode", "code_mode_host", "browser_use", "rmcp_client",
        "multi_agent", "multi_agent_v2", "worktrees", "view_image", "sleep_tool",
        "shell_snapshot", "shell_snapshot_v2", "hooks", "goals", "memories",
        "daemon_auto_start", "auth_elicitation", "tool_suggest",
        "request_permissions_tool", "agent_message_board", "in_app_dictation",
        "enable_mcp_apps", "realtime_conversation", "prevent_idle_sleep",
        "unrelated_feature",
    }
    args = _host_only_feature_args(native_surfaces)
    disabled = set(zip(args[::2], args[1::2]))

    assert {feature for option, feature in disabled if option == "--disable"} == (
        native_surfaces - {"code_mode_host", "unrelated_feature"})
    assert ("--disable", "code_mode_host") not in disabled
    try:
        _host_only_feature_args({"code_mode_host"})
    except RuntimeError as exc:
        assert "native execution" in str(exc)
    else:
        raise AssertionError("launcher must refuse unknown native-execution policy")

    assert _host_only_feature_args({"unified_exec"}) == [
        "--disable", "unified_exec"]


def test_app_server_thread_identity_is_bound_to_host_task_and_recovery_record(tmp_path):
    async def run():
        runtime = _Runtime()
        shared_authority = object()
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: {"ok": True}),),
            model="test-model", cwd=tmp_path, runtime=runtime,
            task_start_options={"state_authority": shared_authority})
        client = _Client()

        status = await adapter.open(client)
        bound_request, start_options = runtime.starts[0]
        recovery = adapter.recovery_record()

        assert status["thread_id"] == "thread-1"
        assert status["app_server_session_id"] == "session-1"
        assert bound_request.interaction_id == "codex-task:task-1"
        assert bound_request.agent_instance_id.endswith(hashlib.sha256(
            recovery["session_nonce"].encode()).hexdigest()[:24])
        assert start_options["session_id"] == recovery["cfw_session_id"]
        assert recovery["cfw_session_id"].startswith("host-cfw-")
        assert start_options["session_nonce"] == recovery["session_nonce"]
        assert start_options["resume"] is False
        assert start_options["state_authority"] is shared_authority
        assert recovery["thread_id"] == "thread-1"
        assert recovery["app_server_session_id"] == "session-1"

        resumed_runtime = _Runtime()
        resumed = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: {"ok": True}),),
            model="test-model", cwd=tmp_path, runtime=resumed_runtime,
            session_nonce=recovery["session_nonce"],
            expected_session_id=recovery["app_server_session_id"],
            task_start_options={"state_authority": shared_authority})
        await resumed.open(_Client(), thread_id=recovery["thread_id"])
        assert resumed_runtime.starts[0][1]["resume"] is True
        assert resumed_runtime.starts[0][1]["state_authority"] is shared_authority
        await adapter.close()
        await resumed.close()

    asyncio.run(run())


def test_jsonl_notification_omits_missing_params_on_wire():
    class _Stdin:
        def __init__(self):
            self.frames = []

        def write(self, value):
            self.frames.append(value)

        async def drain(self):
            return None

        def close(self):
            return None

    class _Process:
        def __init__(self):
            self.stdin = _Stdin()
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def run():
        process = _Process()
        client = JsonlAppServerClient(process)
        await client.notify("initialized")
        await client.notify("test/withParams", {"value": 1})

        assert [json.loads(frame) for frame in process.stdin.frames] == [
            {"method": "initialized"},
            {"method": "test/withParams", "params": {"value": 1}},
        ]
        await client.close()

    asyncio.run(run())


def test_thread_id_is_recovery_identity_when_app_server_omits_session_id(tmp_path):
    async def run():
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: {"ok": True}),),
            model="test-model", cwd=tmp_path, runtime=_Runtime())
        status = await adapter.open(_Client(session_id=None))
        recovery = adapter.recovery_record()

        assert status["thread_id"] == "thread-1"
        assert status["app_server_session_id"] == "thread-1"
        assert status["app_server_identity_kind"] == "thread"
        assert recovery["app_server_identity_kind"] == "thread"

        resumed = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: {"ok": True}),),
            model="test-model", cwd=tmp_path, runtime=_Runtime(),
            session_nonce=recovery["session_nonce"],
            expected_session_id=recovery["app_server_session_id"])
        await resumed.open(_Client(session_id=None), thread_id=recovery["thread_id"])
        assert resumed.status()["app_server_session_id"] == "thread-1"
        assert resumed.status()["app_server_identity_kind"] == "thread"
        await adapter.close()
        await resumed.close()

    asyncio.run(run())


def test_resume_requires_complete_host_recovery_identity(tmp_path):
    binding = _read_binding(lambda _: {"ok": True})
    try:
        CodexAppServerAdapter(
            request=_request(), action_bindings=(binding,), model="test-model",
            cwd=tmp_path, session_nonce="nonce-without-stable-id")
    except ValueError as error:
        assert "both" in str(error)
    else:
        raise AssertionError("resume accepted an incomplete host recovery identity")


def test_task_start_options_cannot_override_adapter_identity(tmp_path):
    try:
        CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: "ok"),),
            model="test-model", cwd=tmp_path,
            task_start_options={"session_id": "caller-selected"})
    except ValueError as error:
        assert "reserved" in str(error)
    else:
        raise AssertionError("task_start_options overrode host recovery identity")


def test_injected_unverified_app_server_does_not_claim_closed_tool_visibility(tmp_path):
    async def run():
        client = _Client()
        client.host_policy_verified = False
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: "ok"),),
            model="test-model", cwd=tmp_path, runtime=_Runtime())
        await adapter.open(client)
        status = adapter.status()
        assert status["model_visible_dp_tools"] is None
        assert status["model_visibility_verified"] is False
        assert status["native_mutation_surfaces_closed"] is None
        await adapter.close()

    asyncio.run(run())


def test_turn_completion_before_start_response_records_failed_attempt(tmp_path):
    async def run():
        host = _Host()
        runtime = _Runtime(host)
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: "ok"),),
            model="test-model", cwd=tmp_path, runtime=runtime)
        client = _Client(early_turn={"id": "turn-1", "status": "failed",
                                    "error": {"message": "provider failed"}})
        await adapter.open(client)

        result = await adapter.run_turn("Read the requested document")

        assert result["status"] == "failed"
        assert client.turn_start_params["sandboxPolicy"] == {"type": "readOnly"}
        assert len(host.attempts) == 1
        assert len(host.failures) == 1
        assert re.fullmatch(r"host-attempt-[a-f0-9]{32}",
                            host.failures[0]["attempt_ref"])
        assert host.attempts[0]["action_plan"] == {
            "action_binding_ids": ["read"]}
        assert re.fullmatch(r"host-cause-[a-f0-9]{32}",
                            host.attempts[0]["cause_id"])
        await adapter.close()

    asyncio.run(run())


def test_dynamic_tool_result_is_sanitized_and_correlated_to_cfw_action(tmp_path):
    async def run():
        host = _Host()
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(
                _read_binding(lambda _: {"text": "ok", "api_key": "should-not-leak"}),),
            model="test-model", cwd=tmp_path, runtime=_Runtime(host))
        await adapter.open(_Client())
        adapter._active_turn_id = "turn-1"
        adapter._active_ready.set()
        params = {"callId": "call-1", "threadId": "thread-1", "turnId": "turn-1",
                  "tool": "cfw_read", "arguments": {"path": "doc.md"}}

        result = await adapter.handle_server_request("item/tool/call", params)
        duplicate = await adapter.handle_server_request("item/tool/call", params)

        assert result["success"] is True
        assert duplicate == result
        assert "should-not-leak" not in result["contentItems"][0]["text"]
        assert host.prepared[0]["tool_binding_id"] == "read"
        assert len(host.prepared) == 1
        assert host.results[0]["action_request_id"] == "action-1"
        assert host.results[0]["invocation_id"] == "call-1"
        assert adapter.status()["cfw_gate_receipts"] == [{
            "stage": "final", "invocation_id": "call-1",
            "action_request_id": "action-1", "action_digest": "",
            "decision": "allow", "reason_codes": ["test_gate"],
            "cfw_revision": None}]
        await adapter.close()

    asyncio.run(run())


@pytest.mark.parametrize(("mutability", "executor_result", "expected_changed"), [
    ("write", {"changed": True}, True),
    ("write", {"artifact_changed": False, "changed": True}, False),
    ("read", {"changed": True}, None),
])
def test_dynamic_write_forwards_explicit_artifact_change_to_host(
        tmp_path, mutability, executor_result, expected_changed):
    async def run():
        host = _Host()
        binding = CodexActionBinding(
            name="cfw_write" if mutability == "write" else "cfw_read",
            description="Host-owned action",
            input_schema={"type": "object", "properties": {
                "path": {"type": "string"}}, "required": ["path"]},
            action_binding_id="write" if mutability == "write" else "read",
            descriptor=ActionDescriptor(
                "write" if mutability == "write" else "read",
                mutability=mutability),
            executor=lambda _: executor_result)
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(binding,),
            model="test-model", cwd=tmp_path, runtime=_Runtime(host))
        await adapter.open(_Client())
        adapter._active_turn_id = "turn-1"
        adapter._active_ready.set()

        result = await adapter.handle_server_request("item/tool/call", {
            "callId": "artifact-observation", "threadId": "thread-1",
            "turnId": "turn-1", "tool": binding.name,
            "arguments": {"path": "src/module.py"}})

        assert result["success"] is True
        actual_result = host.results[0]["actual_result"]
        if expected_changed is None:
            assert "artifact_changed" not in actual_result
        else:
            assert actual_result["artifact_changed"] is expected_changed
        await adapter.close()

    asyncio.run(run())


def test_changed_write_resets_real_cfw_verification_limit(tmp_path):
    async def run():
        verify_descriptor = ActionDescriptor(
            "verify", goal_relation="required_verification",
            affected_criterion_ids=("criterion-1",))
        runtime = CFWRuntime(
            telemetry_disabled=True,
            action_registry={"verify": verify_descriptor},
            workflow_port=_EmptyWorkflowPort())
        write_target = tmp_path / "src" / "module.py"

        def write(_):
            write_target.parent.mkdir(parents=True, exist_ok=True)
            prior = write_target.read_text(encoding="utf-8") if write_target.exists() else None
            write_target.write_text("updated = True\n", encoding="utf-8")
            return {"changed": prior != "updated = True\n"}

        bindings = (
            CodexActionBinding(
                name="cfw_verify", description="Run the host retry check",
                input_schema={"type": "object", "properties": {
                    "suite": {"const": "retry"}}, "required": ["suite"],
                    "additionalProperties": False},
                action_binding_id="verify", descriptor=verify_descriptor,
                executor=lambda _: {"status": "failed"}),
            CodexActionBinding(
                name="cfw_write", description="Write a host-authorized file",
                input_schema={"type": "object", "properties": {
                    "path": {"type": "string"}}, "required": ["path"],
                    "additionalProperties": False},
                action_binding_id="write",
                descriptor=ActionDescriptor("write", mutability="write"),
                executor=write),
        )
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=bindings,
            model="test-model", cwd=tmp_path, runtime=runtime)
        await adapter.open(_Client())
        adapter._active_turn_id = "turn-1"
        adapter._active_ready.set()

        async def call(call_id, tool, arguments):
            return await adapter.handle_server_request("item/tool/call", {
                "callId": call_id, "threadId": "thread-1", "turnId": "turn-1",
                "tool": tool, "arguments": arguments})

        for call_id in ("verify-1", "verify-2"):
            assert (await call(call_id, "cfw_verify", {"suite": "retry"}))["success"]
        blocked = await call("verify-3", "cfw_verify", {"suite": "retry"})
        assert blocked["success"] is False

        write_result = await call("write-1", "cfw_write", {"path": "src/module.py"})
        assert write_result["success"] is True
        assert (await call("verify-4", "cfw_verify", {"suite": "retry"}))["success"]

        final_decisions = [item["decision"] for item in
                           adapter.status()["cfw_gate_receipts"]
                           if item["stage"] == "final"]
        assert final_decisions == ["allow", "allow", "block", "allow", "allow"]
        await adapter.close()

    asyncio.run(run())


def test_dynamic_tool_never_executes_when_cfw_gate_blocks(tmp_path):
    async def run():
        host = _Host(GateDecision.BLOCK)
        executed = []
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(
                lambda args: executed.append(args)),),
            model="test-model", cwd=tmp_path, runtime=_Runtime(host))
        await adapter.open(_Client())
        adapter._active_turn_id = "turn-1"
        adapter._active_ready.set()

        result = await adapter.handle_server_request("item/tool/call", {
            "callId": "call-blocked", "threadId": "thread-1", "turnId": "turn-1",
            "tool": "cfw_read", "arguments": {"path": "doc.md"}})

        assert result["success"] is False
        assert executed == []
        assert host.results == []
        assert host.observations
        assert adapter.status()["cfw_gate_receipts"] == [{
            "stage": "final", "invocation_id": "call-blocked",
            "action_request_id": "", "action_digest": "",
            "decision": "block", "reason_codes": ["test_gate"],
            "cfw_revision": None}]
        await adapter.close()

    asyncio.run(run())


def test_dynamic_tool_action_is_recorded_through_real_cfw_host_api(tmp_path):
    async def run():
        runtime = CFWRuntime(telemetry_disabled=True, workflow_port=_EmptyWorkflowPort())
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(
                lambda args: {"read_path": args["path"], "content": "fixture"}),),
            model="test-model", cwd=tmp_path, runtime=runtime)
        await adapter.open(_Client())
        adapter._active_turn_id = "turn-1"
        adapter._active_ready.set()

        result = await adapter.handle_server_request("item/tool/call", {
            "callId": "real-cfw-call", "threadId": "thread-1", "turnId": "turn-1",
            "tool": "cfw_read", "arguments": {"path": "README.md"}})

        assert result["success"] is True
        assert result["contentItems"][0]["text"] == (
            '{"content":"fixture","read_path":"README.md"}')
        assert adapter._task_handle.session_id.startswith("host-cfw-")
        assert isinstance(adapter._task_handle.base_model_context(), str)
        assert adapter.status()["app_server_session_id"] == "session-1"
        gate_receipts = adapter.status()["cfw_gate_receipts"]
        assert [item["stage"] for item in gate_receipts] == ["preflight", "final"]
        assert all(item["invocation_id"] == "real-cfw-call"
                   and item["cfw_revision"] >= 0
                   and item["decision"] == "allow" for item in gate_receipts)
        await adapter.close()

    asyncio.run(run())


def test_completion_requires_host_gate_validator_evidence_with_real_cfw(tmp_path):
    async def run(outcome):
        goal = TaskGoalContract(
            objective="Run the required test suite",
            deliverable_kind="test_suite", work_mode="delivery",
            acceptance_criteria=(AcceptanceCriterion(
                "suite-1", "The integration suite passes", "test_suite"),))
        request = TaskRequest(task_id="validator-task", application_id="test-app",
                              goal_contract=goal)
        action = CodexActionBinding(
            name="cfw_run_integration_tests", description="Run integration tests",
            input_schema={"type": "object", "properties": {
                "suite": {"const": "integration"}}, "required": ["suite"],
                "additionalProperties": False},
            action_binding_id="test", descriptor=ActionDescriptor("test"),
            executor=lambda _: {"status": outcome, "suite": "integration"})
        validator = CodexValidatorBinding(
            validator_id="integration-suite-v1", action_binding_id="test",
            arguments={"suite": "integration"}, criterion_ids=("suite-1",),
            result_schema={"type": "object", "properties": {
                "status": {"const": "passed"},
                "suite": {"const": "integration"}},
                "required": ["status", "suite"], "additionalProperties": False})
        runtime = CFWRuntime(telemetry_disabled=True,
                             workflow_port=_EmptyWorkflowPort())
        adapter = CodexAppServerAdapter(
            request=request, action_bindings=(action,),
            validator_bindings=(validator,), model="test-model", cwd=tmp_path,
            runtime=runtime)
        client = _Client()
        client.early_turn = {"id": "turn-1", "status": "completed"}
        await adapter.open(client)
        await adapter.run_turn("Finish the task")
        result = await adapter.validate_completion()
        await adapter.close()
        return result

    passed = asyncio.run(run("passed"))
    assert passed["status"] == "completed", passed
    assert passed["completed"] is True
    assert passed["validated_criteria"] == 1

    failed = asyncio.run(run("failed"))
    assert failed["status"] == "incomplete"
    assert failed["decision"] == "validator_failed"


def test_model_run_validator_evidence_is_reused_at_completion(tmp_path):
    async def run():
        calls = []
        suites = ("parser", "retry", "state")
        criteria = tuple(AcceptanceCriterion(
            suite, f"The {suite} suite passes", "test_suite") for suite in suites)
        goal = TaskGoalContract(
            objective="Run the required test suite",
            deliverable_kind="test_suite", work_mode="delivery",
            acceptance_criteria=criteria,
            verification_policy=VerificationPolicy(
                max_verification_actions_without_artifact_change=len(suites)))
        request = TaskRequest(task_id="validator-reuse-task", application_id="test-app",
                              goal_contract=goal)

        def execute(arguments):
            calls.append(dict(arguments))
            return {"status": "passed", "validator": arguments["validator"]}

        action = CodexActionBinding(
            name="cfw_run_registered_suite", description="Run a registered suite",
            input_schema={"type": "object", "properties": {
                "validator": {"enum": list(suites)}}, "required": ["validator"],
                "additionalProperties": False},
            action_binding_id="run_integration_tests",
            descriptor=ActionDescriptor(
                "run_integration_tests", goal_relation="required_verification"),
            executor=execute)
        validators = tuple(CodexValidatorBinding(
            validator_id=f"{suite}-suite-v1",
            action_binding_id="run_integration_tests",
            arguments={"validator": suite}, criterion_ids=(suite,),
            result_schema={"type": "object", "properties": {
                "status": {"const": "passed"},
                "validator": {"const": suite}},
                "required": ["status", "validator"], "additionalProperties": False})
            for suite in suites)
        runtime = CFWRuntime(telemetry_disabled=True,
                             action_registry={"run_integration_tests": action.descriptor},
                             workflow_port=_EmptyWorkflowPort())
        adapter = CodexAppServerAdapter(
            request=request, action_bindings=(action,), validator_bindings=validators,
            model="test-model", cwd=tmp_path, runtime=runtime)
        await adapter.open(_Client())
        adapter._active_turn_id = "turn-validator-reuse"
        adapter._active_ready.set()
        for index, suite in enumerate(suites):
            result = await adapter.handle_server_request("item/tool/call", {
                "callId": f"validator-reuse-call-{index}", "threadId": "thread-1",
                "turnId": "turn-validator-reuse", "tool": "cfw_run_registered_suite",
                "arguments": {"validator": suite}})
            assert result["success"] is True
        adapter._active_turn_id = None
        completion = await adapter.validate_completion()
        await adapter.close()
        return calls, completion, adapter._cfw_gate_receipts

    calls, completion, gate_receipts = asyncio.run(run())
    assert completion["status"] == "completed", json.dumps(completion, sort_keys=True)
    assert [
        {key: item[key] for key in (
            "validator_id", "criterion_ids", "passed", "reused")}
        for item in completion["validators"]
    ] == [
        {"validator_id": f"{suite}-suite-v1", "criterion_ids": [suite],
         "passed": True, "reused": True}
        for suite in ("parser", "retry", "state")]
    for validator in completion["validators"]:
        assert validator["evidence_ref"]
        assert re.fullmatch(r"[a-f0-9]{64}", validator["result_digest"])
        assert re.fullmatch(r"[a-f0-9]{64}", validator["reuse_basis_digest"])
    assert [receipt["stage"] for receipt in gate_receipts] == [
        stage for _ in range(3) for stage in ("preflight", "final")]
    assert all(receipt.get("invocation_id") and receipt.get("action_request_id")
               and receipt.get("decision") == "allow"
               and isinstance(receipt.get("cfw_revision"), int)
               for receipt in gate_receipts if receipt["stage"] == "final")
    assert len(calls) == 3


def test_codex_host_emits_neutral_runtime_boundaries(tmp_path):
    async def run():
        observed = []

        async def observe(event):
            observed.append(event)
            if event.boundary is HarnessBoundary.MODEL_BOUNDARY:
                return {"status": "ready",
                        "context_projection": event.control_data.context + "\nDP guidance"}
            return {"status": "ready"}

        lifecycle = CallbackCognitiveHarnessLifecycle(observe)
        host = _Host()
        action = CodexActionBinding(
            name="run_tests", description="Run one host suite",
            input_schema={"type": "object", "properties": {
                "suite": {"const": "integration"}}, "required": ["suite"],
                "additionalProperties": False},
            action_binding_id="run_integration_tests",
            descriptor=ActionDescriptor("run_integration_tests",
                                        goal_relation="required_verification"),
            executor=lambda _: {"status": "passed"})
        validator = CodexValidatorBinding(
            validator_id="integration-v1", action_binding_id="run_integration_tests",
            arguments={"suite": "integration"}, criterion_ids=("criterion-1",),
            result_schema={"type": "object", "properties": {
                "status": {"const": "passed"}}, "required": ["status"],
                "additionalProperties": False})
        runtime = CFWRuntime(
            telemetry_disabled=True,
            action_registry={"run_integration_tests": action.descriptor},
            workflow_port=_EmptyWorkflowPort())
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(action,),
            validator_bindings=(validator,), model="test-model", cwd=tmp_path,
            runtime=runtime, runtime_harness_lifecycle=lifecycle,
            harness_host_version="codex-fixture-1.0")
        client = _Client(early_turn={"id": "turn-1", "status": "completed"})
        client.codex_feature_catalog = {"dynamic_tools": "fixture-v1"}
        await adapter.open(client)
        await adapter.run_turn("Run the required test suite")
        assert "DP guidance" in client.turn_start_params["input"][0]["text"]
        adapter._active_turn_id = "turn-neutral-action"
        adapter._active_ready.set()
        action_result = await adapter.handle_server_request("item/tool/call", {
            "callId": "neutral-call-1", "threadId": "thread-1",
            "turnId": "turn-neutral-action", "tool": "run_tests",
            "arguments": {"suite": "integration"}})
        assert action_result["success"] is True
        assert any(item.get("stage") == "final"
                   and item.get("decision") == "allow"
                   for item in adapter._cfw_gate_receipts)
        adapter._active_turn_id = None
        completion = await adapter.validate_completion()
        assert completion["status"] == "completed", json.dumps(completion, sort_keys=True)
        await adapter.close()
        return [event.boundary for event in observed]

    assert asyncio.run(run()) == [
        HarnessBoundary.TASK_START,
        HarnessBoundary.MODEL_BOUNDARY,
        HarnessBoundary.ACTION_PROPOSED,
        HarnessBoundary.ACTION_AUTHORIZED,
        HarnessBoundary.VALIDATION_COMPLETED,
        HarnessBoundary.ACTION_COMPLETED,
        HarnessBoundary.COMPLETION_PROPOSED,
        HarnessBoundary.TASK_END,
    ]


def test_codex_adapter_reports_neutral_action_rejection(tmp_path):
    async def run():
        observed = []
        executed = []

        async def observe(event):
            observed.append(event)
            if event.boundary is HarnessBoundary.ACTION_PROPOSED:
                return {"status": "blocked", "decision": "deny"}
            return {"status": "ready"}

        action = _read_binding(lambda args: executed.append(args) or {"ok": True})
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(action,), model="test-model",
            cwd=tmp_path, runtime=_Runtime(),
            runtime_harness_lifecycle=CallbackCognitiveHarnessLifecycle(observe),
            harness_host_version="codex-fixture-1.0")
        await adapter.open(_Client())
        adapter._active_turn_id = "turn-rejected-action"
        adapter._active_ready.set()
        result = await adapter.handle_server_request("item/tool/call", {
            "callId": "rejected-call", "threadId": "thread-1",
            "turnId": "turn-rejected-action", "tool": action.name,
            "arguments": {"path": "doc.md"}})
        boundaries = [event.boundary for event in observed]
        assert result["success"] is False
        assert executed == []
        assert boundaries == [
            HarnessBoundary.TASK_START,
            HarnessBoundary.ACTION_PROPOSED,
            HarnessBoundary.ACTION_REJECTED,
        ]
        await adapter.close()

    asyncio.run(run())


def test_completion_aggregates_cfw_csw_and_runtime_authority_receipts(tmp_path):
    async def run(*, dp_allowed):
        goal = TaskGoalContract(
            objective="Run the required test suite",
            deliverable_kind="test_suite", work_mode="delivery",
            acceptance_criteria=(AcceptanceCriterion(
                "criterion-1", "The integration suite passes", "test_suite"),))
        request = TaskRequest(task_id="completion-authorities", application_id="test-app",
                              goal_contract=goal)
        action = CodexActionBinding(
            name="run_tests", description="Run the integration suite",
            input_schema={"type": "object", "properties": {
                "suite": {"const": "integration"}}, "required": ["suite"],
                "additionalProperties": False},
            action_binding_id="read", descriptor=default_action_registry()["read"],
            executor=lambda _: {"status": "passed"})
        validator = CodexValidatorBinding(
            validator_id="integration-v1", action_binding_id="read",
            arguments={"suite": "integration"}, criterion_ids=("criterion-1",),
            result_schema={"type": "object", "properties": {
                "status": {"const": "passed"}}, "required": ["status"],
                "additionalProperties": False})

        async def observe(event):
            if event.boundary is HarnessBoundary.COMPLETION_PROPOSED:
                return {"status": "ready", "authority_receipts": [{
                    "authority_id": "digital-psychology", "allowed": dp_allowed,
                    "revision": "dp-revision-3",
                    "reason_codes": ([] if dp_allowed else ["dp_completion_denied"]),
                }]}
            return {"status": "ready"}

        lifecycle = CallbackCognitiveHarnessLifecycle(observe)
        lifecycle.completion_authority_ids = ("digital-psychology",)
        host = _Host()
        host.completion_statework_receipt = lambda: {
            "authority_id": "csw-workflows", "allowed": True,
            "revision": "csw-revision-8"}
        runtime = _Runtime(host=host)
        adapter = CodexAppServerAdapter(
            request=request, action_bindings=(action,), validator_bindings=(validator,),
            model="test-model", cwd=tmp_path, runtime=runtime,
            runtime_harness_lifecycle=lifecycle,
            runtime_mode="combined_enforcing",
            harness_host_version="codex-fixture-1.0")
        client = _Client(early_turn={"id": "turn-1", "status": "completed"})
        await adapter.open(client)
        await adapter.run_turn("Run the required test suite")
        completion = await adapter.validate_completion()
        await adapter.close()
        return completion

    allowed = asyncio.run(run(dp_allowed=True))
    assert allowed["status"] == "completed", allowed
    assert allowed["completion_decision"]["allowed"] is True
    assert [item["authority_id"] for item in
            allowed["completion_decision"]["authority_receipts"]] == [
                "cfw-runtime", "csw-workflows", "digital-psychology"]

    denied = asyncio.run(run(dp_allowed=False))
    assert denied["status"] == "incomplete"
    assert denied["completion_decision"]["allowed"] is False
    assert "dp_completion_denied" in denied["completion_decision"]["reason_codes"]


def test_cfw_combined_workflow_receipt_uses_fresh_csw_state():
    from runtime_session.completion import CompletionMethods

    class _StateAuthority:
        completed = True

        def inspect_workflow(self, **_kwargs):
            return SimpleNamespace(completed=self.completed, revision=9)

    class _Revisions:
        observed = []

        def observe_authority(self, revision):
            self.observed.append(revision)

    authority = _StateAuthority()
    revisions = _Revisions()
    session = SimpleNamespace(
        identity_authority=SimpleNamespace(
            authorities=SimpleNamespace(
                runtime_mode="combined_enforcing", state_authority=authority),
            revisions=revisions),
        bundle=SimpleNamespace(
            stateworks=({"id": "workflow-a", "subject_ref": "repo:test",
                         "workflow_contract_hash": "contract-hash"},),
            policy=SimpleNamespace(subject_ref="repo:test")))

    ready = CompletionMethods.completion_statework_receipt(session)
    assert ready["authority_id"] == "csw-workflows"
    assert ready["allowed"] is True
    assert ready["revision"] and revisions.observed == [9]

    authority.completed = False
    blocked = CompletionMethods.completion_statework_receipt(session)
    assert blocked["allowed"] is False
    assert blocked["reason_codes"] == ["csw_workflow_incomplete"]


def test_validator_bindings_must_cover_each_required_criterion(tmp_path):
    goal = TaskGoalContract(
        objective="Verify both outputs", deliverable_kind="test_suite",
        work_mode="delivery", acceptance_criteria=(
            AcceptanceCriterion("one", "first suite", "test_suite"),
            AcceptanceCriterion("two", "second suite", "test_suite")))
    request = TaskRequest(task_id="validator-coverage", application_id="test-app",
                          goal_contract=goal)
    action = CodexActionBinding(
        name="cfw_run_suite", description="Run a test suite",
        input_schema={"type": "object"}, action_binding_id="test",
        descriptor=ActionDescriptor("test"), executor=lambda _: {"status": "passed"})
    validator = CodexValidatorBinding(
        validator_id="first-suite", action_binding_id="test", arguments={},
        criterion_ids=("one",), result_schema={"type": "object"})
    try:
        CodexAppServerAdapter(
            request=request, action_bindings=(action,), validator_bindings=(validator,),
            model="test-model", cwd=tmp_path, runtime=_Runtime())
    except ValueError as error:
        assert "do not cover required task criteria" in str(error)
    else:
        raise AssertionError("validator setup omitted a required criterion")


def test_dp_bridge_connector_requires_same_cfw_identity_and_forwards_boundaries():
    class _DPBridge:
        def __init__(self, authority, identity=None):
            self.identity = identity or SimpleNamespace(
                session_id="host-cfw-stable", task_id="task-1",
                agent_instance_id="agent-stable")
            self.objective = "Read the requested document"
            self.objective_criteria = ("criterion-1",)
            self.cfw = SimpleNamespace(authority_identity=lambda: authority)
            self.events = []

        async def open(self):
            self.events.append("open")
            return {"status": "ready"}

        async def task_started(self):
            self.events.append("task_started")
            return {"status": "ready"}

        async def before_model_call(self, context, *, tool_capable=False):
            self.events.append(("before_model_call", tool_capable))
            return context + " + DP"

        async def before_action_candidate(self, candidate, *, cfw_action_request=None):
            self.events.append(("before_action", candidate, cfw_action_request))
            return {"status": "ready", "action_authorized": True,
                    "action_request_id": "dp-cfw-action"}

        async def after_action(self, observation):
            self.events.append(("after_action", observation))
            return {"status": "ready"}

        async def observe_boundary(self, boundary_kind, *, observation=None):
            self.events.append((boundary_kind, observation))
            return {"status": "ready"}

        async def reconcile(self):
            self.events.append("reconcile")
            return {"status": "ready"}

        async def close(self):
            self.events.append("close")

    authority = {"authority_id": "cfw-same-session"}
    bridges = []
    task_handle = SimpleNamespace(
        agent_instance_id="agent-stable",
        create_component_adapter=lambda _factory: SimpleNamespace(
            authority_identity=lambda: authority))
    host = DigitalPsychologyBridgeHost(
        bridge_factory=lambda **kwargs: bridges.append(_DPBridge(
            authority, SimpleNamespace(
                session_id=kwargs["session_id"],
                task_id=kwargs["request"].task_id,
                agent_instance_id=kwargs["task_handle"].agent_instance_id))) or bridges[-1],
        component_adapter_factory=CFWBehaviorAdapter,
        require_shared_csw=False,
        candidate_factory=lambda **kwargs: {"candidate": kwargs["invocation_id"]},
        observation_factory=lambda **kwargs: {"boundary": kwargs["boundary"]})

    async def run():
        await host.open_task(
            request=_request(), runtime=None, task_handle=task_handle,
            host_handle=None, session_id="host-cfw-stable", session_nonce="nonce",
            resume=False, runtime_mode="standalone_enforcing", thread_id=None,
            app_server_session_id=None)
        bridge = bridges[0]
        await host.bind_transport_identity(
            thread_id="codex-thread", app_server_session_id="codex-session")
        assert host.transport_identity == {
            "thread_id": "codex-thread", "app_server_session_id": "codex-session"}
        await host.task_started()
        context = await host.before_model_call(context="CFW", tool_capable=True)
        decision = await host.before_action(
            action={"tool_binding_id": "read"}, binding="read",
            invocation_id="call-1")
        await host.after_action(
            action={"tool_binding_id": "read"}, binding="read",
            invocation_id="call-1", action_request_id="dp-cfw-action",
            actual_result={"success": True}, result_digest="d" * 64,
            evidence_ref="ev-1")
        await host.observe_validator(
            validator_id="validator", result={"valid": True},
            criterion_evidence_refs={"criterion-1": ("ev-1",)})
        await host.before_completion(
            validator_id="validator", result={"valid": True},
            criterion_evidence_refs={"criterion-1": ("ev-1",)})
        assert context == "CFW + DP"
        assert decision["action_authorized"] is True
        assert bridge.events[0] == "open"
        assert any(isinstance(event, tuple) and event[0] == "pre_completion"
                   for event in bridge.events)
        await host.close()

        mismatched = _DPBridge({"authority_id": "different-session"})
        rejected = DigitalPsychologyBridgeHost(
            mismatched,
            component_adapter_factory=CFWBehaviorAdapter,
            require_shared_csw=False,
            candidate_factory=lambda **kwargs: kwargs,
            observation_factory=lambda **kwargs: kwargs)
        try:
            await rejected.open_task(
                request=_request(), runtime=None, task_handle=task_handle,
                host_handle=None, session_id="host-cfw-stable", session_nonce="nonce",
                resume=False, runtime_mode="standalone_enforcing", thread_id=None,
                app_server_session_id=None)
        except RuntimeError as error:
            assert "different CFW task" in str(error)
        else:
            raise AssertionError("DP bridge accepted a different CFW authority")
        assert mismatched.events == []

    asyncio.run(run())


def test_dp_triad_connector_rejects_standalone_cfw_before_open():
    authority = {"authority_id": "cfw-same-session"}
    bridge = SimpleNamespace(
        identity=SimpleNamespace(
            session_id="host-cfw-stable", task_id="task-1",
            agent_instance_id="agent-stable"),
        objective="Read the requested document",
        objective_criteria=("criterion-1",),
        cfw=SimpleNamespace(authority_identity=lambda: authority),
        csw=object(), events=[])

    async def open_bridge():
        bridge.events.append("opened")
        return {"status": "ready"}

    bridge.open = open_bridge
    task_handle = SimpleNamespace(
        agent_instance_id="agent-stable",
        create_component_adapter=lambda _factory: SimpleNamespace(
            authority_identity=lambda: authority,
            workflow_authority_identities=lambda: {"statework-a": {"identity": "pinned"}}))
    connector = DigitalPsychologyBridgeHost(
        bridge, component_adapter_factory=CFWBehaviorAdapter,
        candidate_factory=lambda **kwargs: kwargs,
        observation_factory=lambda **kwargs: kwargs)

    async def run():
        try:
            await connector.open_task(
                request=_request(), runtime=None, task_handle=task_handle,
                host_handle=None, session_id="host-cfw-stable", session_nonce="nonce",
                resume=False, runtime_mode="standalone_enforcing", thread_id=None,
                app_server_session_id=None)
        except RuntimeError as error:
            assert "combined_enforcing" in str(error)
        else:
            raise AssertionError("triad connector accepted standalone CFW mode")
        assert bridge.events == []

        missing_workflows = SimpleNamespace(
            identity=bridge.identity,
            objective=bridge.objective,
            objective_criteria=bridge.objective_criteria,
            cfw=bridge.cfw, csw=object(), events=[])

        async def open_without_bindings():
            missing_workflows.events.append("opened")
            return {"status": "ready"}

        missing_workflows.open = open_without_bindings
        empty_task_handle = SimpleNamespace(
            agent_instance_id="agent-stable",
            create_component_adapter=lambda _factory: SimpleNamespace(
                authority_identity=lambda: authority,
                workflow_authority_identities=dict))
        empty_connector = DigitalPsychologyBridgeHost(
            missing_workflows, component_adapter_factory=CFWBehaviorAdapter,
            candidate_factory=lambda **kwargs: kwargs,
            observation_factory=lambda **kwargs: kwargs)
        try:
            await empty_connector.open_task(
                request=_request(), runtime=None, task_handle=empty_task_handle,
                host_handle=None, session_id="host-cfw-stable", session_nonce="nonce",
                resume=False, runtime_mode="combined_enforcing", thread_id=None,
                app_server_session_id=None)
        except RuntimeError as error:
            assert "no shared CSW workflow authority bindings" in str(error)
        else:
            raise AssertionError("triad connector accepted an unbound CFW workflow set")
        assert missing_workflows.events == []

    asyncio.run(run())


def test_dp_triad_status_exposes_verified_shared_authorities():
    authority = {"authority_id": "cfw-same-session"}
    workflow_identity = {"workflow_id": "statework-a", "registry_hash": "sha256:abc"}
    bridge = SimpleNamespace(
        identity=SimpleNamespace(
            session_id="host-cfw-stable", task_id="task-1",
            agent_instance_id="agent-stable"),
        objective="Read the requested document",
        objective_criteria=("criterion-1",),
        cfw=SimpleNamespace(authority_identity=lambda: authority),
        csw=object(),
        behavior_state=SimpleNamespace(
            revision=5,
            active_interventions=(SimpleNamespace(intervention_id="intervention-1"),),
            intervention_history=(
                SimpleNamespace(intervention_id="intervention-1"),
                SimpleNamespace(intervention_id="intervention-2"))),
        fault=None,
        production_metrics_snapshot=lambda: {
            "counters": {"control_ticks_committed": 3,
                         "attention_rotations": 2},
            "timers": {"control_tick_prepare_duration_ms": {
                "count": 3, "total_ms": 12.5, "max_ms": 5.0}},
        })

    async def open_bridge():
        return {"status": "ready"}

    async def close_bridge():
        return None

    bridge.open = open_bridge
    bridge.close = close_bridge
    behavior_adapter = SimpleNamespace(
        authority_identity=lambda: authority,
        workflow_authority_identities=lambda: {"statework-a": workflow_identity},
        lifecycle_revision=4)
    task_handle = SimpleNamespace(
        agent_instance_id="agent-stable",
        create_component_adapter=lambda _factory: behavior_adapter)
    connector = DigitalPsychologyBridgeHost(
        bridge, component_adapter_factory=CFWBehaviorAdapter,
        candidate_factory=lambda **kwargs: kwargs,
        observation_factory=lambda **kwargs: kwargs)

    async def run():
        await connector.open_task(
            request=_request(), runtime=None, task_handle=task_handle,
            host_handle=None, session_id="host-cfw-stable", session_nonce="nonce",
            resume=False, runtime_mode="combined_enforcing", thread_id=None,
            app_server_session_id=None)
        status = connector.status_snapshot()
        assert status["status"] == "ready"
        assert status["private_dp_bridge_open"] is True
        assert status["cfw_authority_verified"] is True
        assert status["shared_csw_authority_verified"] is True
        assert status["shared_csw_workflow_ids"] == ["statework-a"]
        assert status["cfw_behavior_revision"] == 4
        assert status["dp_behavior_revision"] == 5
        assert status["dp_intervention_count"] == 2
        assert status["dp_intervention_count_semantics"].startswith("unique intervention IDs")
        assert status["dp_intervention_creations_by_origin"] == {
            "startup": 2, "runtime_behavior": 0, "post_action": 0,
            "completion": 0}
        assert status["dp_intervention_creations_semantics"].startswith(
            "unique intervention IDs")
        assert status["dp_intervention_metrics"] == {
            "unique_created": 2, "active": 1, "applications": 2,
            "closed": 2, "effective": 0, "ineffective": 0,
            "unknown": 2,
            "created_by_origin": {
                "startup": 2, "runtime_behavior": 0, "post_action": 0,
                "completion": 0},
            "semantics": (
                "unique_created counts distinct IDs first observed by this bridge; "
                "applications counts distinct IDs retained in active state or outcome "
                "history; closed/outcome counts cover retained history only"),
        }
        assert status["production_metrics"]["counters"][
            "attention_rotations"] == 2
        await connector.close()
        assert connector.status_snapshot()["status"] == "closed"

    asyncio.run(run())


def test_generic_codex_dp_connector_uses_the_live_cfw_task_gate(tmp_path):
    class _PrivateDPCoordinator:
        def __init__(self, *, request, task_handle, session_id):
            self.identity = SimpleNamespace(
                session_id=session_id, task_id=request.task_id,
                agent_instance_id=task_handle.agent_instance_id)
            self.objective = request.goal_contract.objective
            self.objective_criteria = tuple(
                item.criterion_id for item in request.goal_contract.acceptance_criteria)
            self.cfw = task_handle.create_component_adapter(CFWBehaviorAdapter)
            self.behavior_state = SimpleNamespace(
                revision=3, active_interventions=(), intervention_history=())
            self.fault = None
            self.events = []

        def production_metrics_snapshot(self):
            return {"counters": {"attention_rotations": 1}, "timers": {}}

        async def open(self):
            self.events.append("open")
            return {"status": "ready"}

        async def task_started(self):
            self.events.append("task_started")
            return {"status": "ready"}

        async def before_model_call(self, context, *, tool_capable=False):
            self.events.append("pre_model")
            projected = self.cfw.before_model_call(
                context, tool_capable=tool_capable)
            return f"{projected} + DP"

        async def before_action_candidate(self, candidate, *, cfw_action_request=None):
            self.events.append(("pre_action", candidate))
            preflight = self.cfw.preflight_action(cfw_action_request)
            if preflight.get("decision") != "allow":
                return {"status": "ready", "action_authorized": False}
            final = self.cfw.authorize_action(cfw_action_request)
            allowed = final.get("decision") == "allow"
            return {"status": "ready", "action_authorized": allowed,
                    "action_request_id": final.get("action_request_id")}

        async def after_action(self, observation):
            self.events.append(("post_action", observation))
            return {"status": "ready"}

        async def observe_boundary(self, boundary_kind, *, observation=None):
            self.events.append((boundary_kind, observation))
            return {"status": "ready"}

        async def reconcile(self):
            return {"status": "ready"}

        async def close(self):
            self.events.append("close")

    async def run():
        runtime = CFWRuntime(telemetry_disabled=True,
                             workflow_port=_EmptyWorkflowPort())
        coordinators = []

        def build_private_dp(**kwargs):
            bridge = _PrivateDPCoordinator(
                request=kwargs["request"], task_handle=kwargs["task_handle"],
                session_id=kwargs["session_id"])
            coordinators.append(bridge)
            return bridge

        cognitive = DigitalPsychologyHarnessLifecycle(
            bridge_factory=build_private_dp,
            component_adapter_factory=CFWBehaviorAdapter,
            require_shared_csw=False,
            candidate_factory=lambda **kwargs: {
                "invocation_id": kwargs["invocation_id"],
                "binding": kwargs["binding"]},
            observation_factory=lambda **kwargs: {
                "boundary": kwargs["boundary"],
                "evidence_ref": kwargs.get("evidence_ref")})
        adapter = CodexAppServerAdapter(
            request=_request(),
            action_bindings=(_read_binding(
                lambda args: {"read_path": args["path"], "content": "fixture"}),),
            model="test-model", cwd=tmp_path, runtime=runtime,
            runtime_harness_lifecycle=cognitive)
        client = _Client()
        await adapter.open(client)
        client.early_turn = {"id": "turn-1", "status": "completed"}
        await adapter.run_turn("Read the requested document")

        adapter._active_turn_id = "turn-action"
        adapter._active_ready.set()
        result = await adapter.handle_server_request("item/tool/call", {
            "callId": "live-cfw-call", "threadId": "thread-1",
            "turnId": "turn-action", "tool": "cfw_read",
            "arguments": {"path": "README.md"}})

        assert result["success"] is True
        assert result["contentItems"][0]["text"] == (
            '{"content":"fixture","read_path":"README.md"}')
        assert any(isinstance(event, tuple) and event[0] == "pre_action"
                   for event in coordinators[0].events)
        assert any(isinstance(event, tuple) and event[0] == "post_action"
                   for event in coordinators[0].events)
        assert adapter.status()["model_visible_dp_tools"] == 0
        assert adapter.status()["native_mutation_surfaces_closed"] is True
        assert adapter.status()["runtime_harness_lifecycle_connected"] is True
        assert " + DP" in client.turn_start_params["input"][0]["text"]
        runtime_status = adapter.status()["runtime_harness_lifecycle_status"]
        assert runtime_status["private_dp_bridge_open"] is True
        assert runtime_status["cfw_authority_verified"] is True
        assert runtime_status["dp_behavior_revision"] == 3
        assert runtime_status["production_metrics"]["counters"][
            "attention_rotations"] == 1
        await adapter.close()

    asyncio.run(run())


def test_matched_baseline_treatment_fixture_contract_trials(tmp_path):
    fixture = json.loads((ROOT / "evaluation_fixtures" /
                          "codex-app-server-host-v1.json").read_text(encoding="utf-8"))

    async def run_cohort(name, cohort, *, offset):
        executions = []
        rows = []
        decision = GateDecision(cohort["gate_decision"])
        for index in range(fixture["trials_per_cohort"]):
            trial_index = offset + index
            trial_id = f"{fixture['shared']['task_id_prefix']}-{name}-{index}"
            host = _Host(decision)
            binding = CodexActionBinding(
                name=fixture["shared"]["tool"], description="Fixture read",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}},
                              "required": ["path"]},
                action_binding_id=fixture["shared"]["action_binding_id"],
                descriptor=ActionDescriptor("read"),
                executor=lambda args, trial_id=trial_id: (
                    executions.append((trial_id, args["path"])) or {
                        "path": args["path"]}),
            )
            request = TaskRequest(task_id=trial_id, application_id="fixture-app")
            adapter = CodexAppServerAdapter(
                request=request, action_bindings=(binding,), model="fixture-model",
                cwd=tmp_path, runtime=_Runtime(host))
            client = _Client(thread_id=f"thread-{trial_index}",
                             session_id=f"session-{trial_index}")
            await adapter.open(client)
            adapter._active_turn_id = f"turn-{trial_index}"
            adapter._active_ready.set()
            result = await adapter.handle_server_request("item/tool/call", {
                "callId": f"call-{trial_index}",
                "threadId": f"thread-{trial_index}",
                "turnId": f"turn-{trial_index}",
                "tool": fixture["shared"]["tool"],
                "arguments": dict(fixture["shared"]["arguments"]),
            })
            rows.append({"result": result,
                         "executed": any(entry[0] == trial_id for entry in executions),
                         "task_id": trial_id})
            await adapter.close()
        return executions, rows

    async def run():
        baseline_executions, baseline = await run_cohort(
            "baseline", fixture["cohorts"]["baseline"], offset=0)
        treatment_executions, treatment = await run_cohort(
            "treatment", fixture["cohorts"]["treatment"],
            offset=fixture["trials_per_cohort"])
        holdout_executions, holdout = await run_cohort(
            "holdout", fixture["holdout"],
            offset=2 * fixture["trials_per_cohort"])

        assert len(baseline) == fixture["trials_per_cohort"]
        assert len(treatment) == fixture["trials_per_cohort"]
        assert len(holdout) == fixture["trials_per_cohort"]
        assert all(item["result"]["success"] and item["executed"]
                   for item in baseline)
        assert all(not item["result"]["success"] and not item["executed"]
                   for item in treatment)
        assert len(baseline_executions) == fixture["cohorts"]["baseline"][
            "expected_action_executions"]
        assert len(treatment_executions) == fixture["cohorts"]["treatment"][
            "expected_action_executions"]
        assert all(item["result"]["success"] and item["executed"]
                   for item in holdout)
        assert len(holdout_executions) == fixture["trials_per_cohort"]
        assert fixture["claims"]["models_used"] is False
        assert fixture["claims"]["efficacy_inference_allowed"] is False

    asyncio.run(run())


def _read_binding(executor, *, name="cfw_read"):
    return CodexActionBinding(
        name=name, description="Read a host-authorized resource",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]},
        action_binding_id="read", descriptor=ActionDescriptor("read"),
        executor=executor)


def test_codex_dynamic_tool_names_are_host_selected_without_product_prefix():
    binding = _read_binding(lambda _: {"ok": True}, name="read")

    assert binding.app_server_spec()["name"] == "read"
    with pytest.raises(ValueError, match="bounded safe identifier"):
        _read_binding(lambda _: {"ok": True}, name="../outside")


def test_codex_bindings_register_through_neutral_runtime_contracts(tmp_path):
    action = _read_binding(lambda _: {"ok": True})
    validator = CodexValidatorBinding(
        validator_id="read-check-v1", action_binding_id="read",
        arguments={"path": "doc.md"}, criterion_ids=("criterion-1",),
        result_schema={"type": "object"})
    adapter = CodexAppServerAdapter(
        request=_request(), action_bindings=(action,),
        validator_bindings=(validator,), model="test-model", cwd=tmp_path)

    assert isinstance(action.host_binding, HostActionBinding)
    assert isinstance(validator.host_binding, HostValidatorBinding)
    assert adapter.host_runtime_bindings.action_bindings_by_id["read"] == action.host_binding
    assert isinstance(adapter.validator_bindings[0], HostValidatorBinding)
