from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from codex_app_server_adapter import (
    CodexActionBinding,
    CodexAppServerAdapter,
    CodexCognitiveHost,
    CodexValidatorBinding,
    JsonlAppServerClient,
    _host_only_feature_args,
)
from codex_app_server_dp_bridge import DigitalPsychologyBridgeHost
from codex_host.launch import CodexLaunchProfile
from codex_host.readiness import CodexInfrastructureUnavailable
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
        cognitive = _CognitiveHost(events)
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: {"ok": True}),),
            model="test-model", cwd=tmp_path, runtime=runtime,
            cognitive_host=cognitive,
            launch_profile=CodexLaunchProfile(
                binary="/packaged/codex", auth_mode="chatgpt",
                codex_home=tmp_path / "temporary-codex-home"))
        client = _RoutingFailureClient(events=events)
        with pytest.raises(CodexInfrastructureUnavailable) as captured:
            await adapter.open(client)
        assert captured.value.kind == "workspace_routing_unavailable"
        assert runtime.starts == []
        assert "cognitive_open" not in events
        assert "task_started" not in events
        assert "thread/start" not in events
        assert client.closed is True

    asyncio.run(run())


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
        assert recovery["cfw_session_id"].startswith("codex-cfw-")
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
        assert host.failures[0]["attempt_ref"] == "codex-turn:turn-1"
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
        assert adapter._task_handle.session_id.startswith("codex-cfw-")
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


class _CognitiveHost:
    def __init__(self, events):
        self.events = events
        self.actions = []
        self.validators = []
        self.after_result = {"status": "ready"}

    async def open_task(self, **kwargs):
        self.events.append("cognitive_open")
        self.open_options = kwargs
        return {"status": "ready"}

    async def task_started(self):
        self.events.append("task_started")
        return {"status": "ready"}

    async def bind_transport_identity(self, **kwargs):
        self.events.append("transport_bound")
        self.transport_identity = kwargs
        return {"status": "ready"}

    async def before_model_call(self, *, context, tool_capable):
        self.events.append("before_model_call")
        self.model_context = context
        return f"{context}\nDP+CSW projection"

    async def before_action(self, **kwargs):
        self.events.append("before_action")
        self.actions.append(kwargs)
        return {"action_authorized": True, "action_request_id": "triad-action-1"}

    async def after_action(self, **kwargs):
        self.events.append("after_action")
        self.after_options = kwargs
        return self.after_result

    async def observe_validator(self, **kwargs):
        self.events.append("observe_validator")
        self.validators.append(kwargs)
        return {"status": "ready"}

    async def before_completion(self, **kwargs):
        self.events.append("before_completion")
        return {"status": "ready", "decision": "allow"}

    async def reconcile(self):
        self.events.append("reconcile")
        return {"status": "ready"}

    async def close(self):
        self.events.append("cognitive_close")


def test_cognitive_host_lifecycle_is_bracketed_around_generic_app_server(tmp_path):
    async def run():
        events = []
        runtime = _Runtime(events=events)
        cognitive: CodexCognitiveHost = _CognitiveHost(events)
        adapter = CodexAppServerAdapter(
            request=_request(),
            action_bindings=(_read_binding(lambda _: {"ok": True}),),
            model="test-model", cwd=tmp_path, runtime=runtime,
            cognitive_host=cognitive)

        await adapter.open(_Client(events=events))
        assert events.index("cfw_task_start") < events.index("cognitive_open")
        assert events.index("cognitive_open") < events.index("task_started")
        assert events.index("task_started") < events.index("thread/start")
        assert events.index("thread/start") < events.index("transport_bound")
        assert callable(getattr(cognitive, "before_action", None))
        params = adapter.thread_start_params()
        assert "mcpServers" not in params and "mcp_servers" not in params
        assert all(item["name"].startswith("cfw_")
                   for item in params["dynamicTools"])
        assert cognitive.transport_identity == {
            "thread_id": "thread-1", "app_server_session_id": "session-1"}
        assert adapter.status()["internal_inference_boundaries_intercepted"] is False
        assert adapter.status()["model_visible_dp_tools"] == 0
        assert adapter.status()["model_visibility_verified"] is True
        assert adapter.status()["native_mutation_surfaces_closed"] is True
        await adapter.close()

    asyncio.run(run())


def test_cognitive_host_brackets_model_action_and_completion_once(tmp_path):
    async def run():
        events = []
        host = _Host()
        cognitive = _CognitiveHost(events)
        validator_action = CodexActionBinding(
            name="cfw_run_tests", description="Run the host test suite",
            input_schema={"type": "object", "properties": {
                "suite": {"type": "string"}}, "required": ["suite"],
                "additionalProperties": False},
            action_binding_id="test", descriptor=ActionDescriptor("test"),
            executor=lambda args: {"status": "passed", "suite": args["suite"]})
        validator = CodexValidatorBinding(
            validator_id="integration-tests-v1", action_binding_id="test",
            arguments={"suite": "integration"}, criterion_ids=("criterion-1",),
            result_schema={"type": "object", "properties": {
                "status": {"const": "passed"},
                "suite": {"const": "integration"}},
                "required": ["status", "suite"], "additionalProperties": False})
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(
                _read_binding(lambda _: {"ok": True}), validator_action),
            validator_bindings=(validator,),
            model="test-model", cwd=tmp_path, runtime=_Runtime(host, events),
            cognitive_host=cognitive)
        client = _Client(events=events)
        await adapter.open(client)

        # The fake transport completes a turn immediately so the test verifies
        # the host boundary call and the context sent to the App Server.
        client.early_turn = {"id": "turn-1", "status": "completed"}
        result = await adapter.run_turn("Read the requested document")
        assert result["status"] == "completed"
        assert cognitive.model_context == "base runtime guidance"
        assert "DP+CSW projection" in client.turn_start_params["input"][0]["text"]
        assert events.count("before_model_call") == 1
        assert adapter._task_handle.model_calls == 0

        adapter._active_turn_id = "turn-action"
        adapter._active_ready.set()
        tool_result = await adapter.handle_server_request("item/tool/call", {
            "callId": "triad-call", "threadId": "thread-1", "turnId": "turn-action",
            "tool": "cfw_read", "arguments": {"path": "doc.md"}})
        assert tool_result["success"] is True
        assert len(host.prepared) == 0
        assert cognitive.actions[0]["invocation_id"] == "triad-call"
        assert cognitive.after_options["action_request_id"] == "triad-action-1"
        assert events.index("before_action") < events.index("after_action")

        active_completion = await adapter.validate_completion()
        assert active_completion["decision"] == "turn_in_progress"
        assert not cognitive.validators
        adapter._active_turn_id = None
        # The host runs a fixed validator action; the model cannot supply a
        # pass boolean or arbitrary evidence references.
        completion = await adapter.validate_completion()
        assert completion["status"] == "completed"
        assert events.index("before_action") < events.index("after_action")
        assert events.index("observe_validator") < events.index("before_completion")
        await adapter.close()

    asyncio.run(run())


def test_model_invoked_registered_validator_records_cfw_and_dp_evidence(tmp_path):
    async def run():
        events = []
        host = _Host()
        cognitive = _CognitiveHost(events)
        criterion = "integration"
        goal = TaskGoalContract(
            objective="Validate the integration behavior",
            deliverable_kind="code_behavior", work_mode="delivery",
            acceptance_criteria=(AcceptanceCriterion(
                criterion, "The integration validator passes", "code_behavior",
                required_boundaries=("validator:integration",)),))
        request = TaskRequest(task_id="dynamic-validator-task",
                              application_id="test-app", goal_contract=goal)
        action = CodexActionBinding(
            name="cfw_run_integration", description="Run the integration validator",
            input_schema={"type": "object", "properties": {
                "suite": {"const": "integration"}}, "required": ["suite"],
                "additionalProperties": False},
            action_binding_id="test", descriptor=ActionDescriptor("test"),
            executor=lambda _: {"status": "passed", "suite": "integration"})
        validator = CodexValidatorBinding(
            validator_id="integration-tests-v1", action_binding_id="test",
            arguments={"suite": "integration"}, criterion_ids=(criterion,),
            covered_boundary_ids=("validator:integration",),
            result_schema={"type": "object", "properties": {
                "status": {"const": "passed"},
                "suite": {"const": "integration"}},
                "required": ["status", "suite"], "additionalProperties": False})
        adapter = CodexAppServerAdapter(
            request=request, action_bindings=(action,), validator_bindings=(validator,),
            model="test-model", cwd=tmp_path, runtime=_Runtime(host, events),
            cognitive_host=cognitive)
        await adapter.open(_Client(events=events))
        adapter._active_turn_id = "turn-validator"
        adapter._active_ready.set()
        result = await adapter.handle_server_request("item/tool/call", {
            "callId": "validator-call", "threadId": "thread-1",
            "turnId": "turn-validator", "tool": "cfw_run_integration",
            "arguments": {"suite": "integration"}})
        assert result["success"] is True
        assert host.validator_evidence == [{
            "validator_id": "completion-boundary-v1",
            "invocation_id": "validator-call",
            "result": {"status": "passed", "suite": "integration"},
            "subject_ref": request.task_id,
            "claim_digest": host.validator_evidence[0]["claim_digest"],
            "covered_boundary_ids": ("validator:integration",),
        }]
        assert host.validator_evidence[0]["claim_digest"]
        assert cognitive.validators == [{
            "validator_id": "integration-tests-v1",
            "result": {"status": "passed", "suite": "integration"},
            "criterion_evidence_refs": {
                criterion: ("validator-evidence-1",)},
        }]
        assert events.index("before_action") < events.index("after_action")
        assert events.index("after_action") < events.index("observe_validator")
        await adapter.close()

    asyncio.run(run())


def test_cognitive_post_action_failure_pauses_following_turns(tmp_path):
    async def run():
        events = []
        cognitive = _CognitiveHost(events)
        cognitive.after_result = {"status": "blocked"}
        adapter = CodexAppServerAdapter(
            request=_request(), action_bindings=(_read_binding(lambda _: {"ok": True}),),
            model="test-model", cwd=tmp_path, runtime=_Runtime(events=events),
            cognitive_host=cognitive)
        await adapter.open(_Client(events=events))
        adapter._active_turn_id = "turn-1"
        adapter._active_ready.set()
        result = await adapter.handle_server_request("item/tool/call", {
            "callId": "triad-call", "threadId": "thread-1", "turnId": "turn-1",
            "tool": "cfw_read", "arguments": {"path": "doc.md"}})
        assert result["success"] is False
        assert adapter.status()["cognitive_fault"] == "post_action_boundary_failed"
        try:
            await adapter.run_turn("continue")
        except RuntimeError as error:
            assert "reconciliation" in str(error)
        else:
            raise AssertionError("model turn continued after failed post-action boundary")
        await adapter.close()

    asyncio.run(run())


def test_dp_bridge_connector_requires_same_cfw_identity_and_forwards_boundaries():
    class _DPBridge:
        def __init__(self, authority, identity=None):
            self.identity = identity or SimpleNamespace(
                session_id="codex-cfw-stable", task_id="task-1",
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
        digital_psychology_adapter=lambda: SimpleNamespace(
            authority_identity=lambda: authority))
    host = DigitalPsychologyBridgeHost(
        bridge_factory=lambda **kwargs: bridges.append(_DPBridge(
            authority, SimpleNamespace(
                session_id=kwargs["session_id"],
                task_id=kwargs["request"].task_id,
                agent_instance_id=kwargs["task_handle"].agent_instance_id))) or bridges[-1],
        require_shared_csw=False,
        candidate_factory=lambda **kwargs: {"candidate": kwargs["invocation_id"]},
        observation_factory=lambda **kwargs: {"boundary": kwargs["boundary"]})

    async def run():
        await host.open_task(
            request=_request(), runtime=None, task_handle=task_handle,
            host_handle=None, session_id="codex-cfw-stable", session_nonce="nonce",
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
            require_shared_csw=False,
            candidate_factory=lambda **kwargs: kwargs,
            observation_factory=lambda **kwargs: kwargs)
        try:
            await rejected.open_task(
                request=_request(), runtime=None, task_handle=task_handle,
                host_handle=None, session_id="codex-cfw-stable", session_nonce="nonce",
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
            session_id="codex-cfw-stable", task_id="task-1",
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
        digital_psychology_adapter=lambda: SimpleNamespace(
            authority_identity=lambda: authority,
            workflow_authority_identities=lambda: {"statework-a": {"identity": "pinned"}}))
    connector = DigitalPsychologyBridgeHost(
        bridge, candidate_factory=lambda **kwargs: kwargs,
        observation_factory=lambda **kwargs: kwargs)

    async def run():
        try:
            await connector.open_task(
                request=_request(), runtime=None, task_handle=task_handle,
                host_handle=None, session_id="codex-cfw-stable", session_nonce="nonce",
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
            digital_psychology_adapter=lambda: SimpleNamespace(
                authority_identity=lambda: authority,
                workflow_authority_identities=dict))
        empty_connector = DigitalPsychologyBridgeHost(
            missing_workflows, candidate_factory=lambda **kwargs: kwargs,
            observation_factory=lambda **kwargs: kwargs)
        try:
            await empty_connector.open_task(
                request=_request(), runtime=None, task_handle=empty_task_handle,
                host_handle=None, session_id="codex-cfw-stable", session_nonce="nonce",
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
            session_id="codex-cfw-stable", task_id="task-1",
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
        digital_psychology_adapter=lambda: behavior_adapter)
    connector = DigitalPsychologyBridgeHost(
        bridge, candidate_factory=lambda **kwargs: kwargs,
        observation_factory=lambda **kwargs: kwargs)

    async def run():
        await connector.open_task(
            request=_request(), runtime=None, task_handle=task_handle,
            host_handle=None, session_id="codex-cfw-stable", session_nonce="nonce",
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
            self.cfw = task_handle.digital_psychology_adapter()
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
            return self.cfw.before_model_call(context, tool_capable=tool_capable)

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

        cognitive = DigitalPsychologyBridgeHost(
            bridge_factory=build_private_dp,
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
            cognitive_host=cognitive)
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
        cognitive_status = adapter.status()["cognitive_host_status"]
        assert cognitive_status["private_dp_bridge_open"] is True
        assert cognitive_status["cfw_authority_verified"] is True
        assert cognitive_status["dp_behavior_revision"] == 3
        assert cognitive_status["production_metrics"]["counters"][
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


def _read_binding(executor):
    return CodexActionBinding(
        name="cfw_read", description="Read a host-authorized resource",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]},
        action_binding_id="read", descriptor=ActionDescriptor("read"),
        executor=executor)
