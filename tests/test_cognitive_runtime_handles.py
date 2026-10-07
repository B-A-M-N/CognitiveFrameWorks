from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_host.handles import (AgentSessionHandle, HostSessionHandle,
                                  RuntimeTaskHandle)
from runtime_host.host_api import CognitiveRuntime as RuntimeHostAPI
from runtime_host.interceptor import install_host_interceptor
from runtime_host.session_registry import HOST_SESSION_REGISTRY
from runtime_contract.component_control import COMPONENT_BASELINE_MODES
from cognitive_runtime import CognitiveRuntime


def test_runtime_handles_live_outside_the_interceptor_module() -> None:
    assert AgentSessionHandle.__module__ == "runtime_host.handles"
    assert RuntimeTaskHandle.__module__ == "runtime_host.handles"
    assert HostSessionHandle.__module__ == "runtime_host.handles"


def test_agent_and_task_handles_do_not_carry_host_authority() -> None:
    agent = AgentSessionHandle("unresolved-test-token")
    task = RuntimeTaskHandle("unresolved-test-token")

    assert not hasattr(agent, "after_tool")
    assert not hasattr(agent, "finish_task")
    assert not hasattr(task, "after_tool")
    assert not hasattr(task, "_host_api")
    assert not hasattr(task, "digital_psychology_adapter")


def test_host_handle_accepts_the_runtime_host_api_explicitly() -> None:
    host_api = object()
    handle = HostSessionHandle("unresolved-test-token", host_api=host_api)

    assert handle._host_api() is host_api


def test_task_handle_uses_a_caller_selected_component_adapter_factory(monkeypatch) -> None:
    session = object()
    monkeypatch.setattr(HOST_SESSION_REGISTRY, "resolve", lambda _token: session)
    task = RuntimeTaskHandle("component-adapter-test")
    calls = []

    def factory(received_session, agent_handle):
        calls.append((received_session, agent_handle))
        return object()

    first = task.create_component_adapter(factory)
    second = task.create_component_adapter(factory)

    assert first is second
    assert len(calls) == 1
    assert calls[0][0] is session
    assert isinstance(calls[0][1], AgentSessionHandle)


def test_behavioral_trace_snapshot_includes_effective_component_modes(monkeypatch) -> None:
    lifecycle = SimpleNamespace(
        component_mode=lambda component_id: "passive" if component_id == "owl" else None)
    session = SimpleNamespace(
        _evaluate_standalone_behavior=lambda: SimpleNamespace(
            mode="DELIVER", active_criterion_ids=(),
            goal_reanchor_required=False,
            reason="required criterion remains unfinished"),
        identity_authority=SimpleNamespace(
            identity=SimpleNamespace(attempt_id="attempt-1")),
        bundle=SimpleNamespace(
            pinned={"goal_contract_hash": "a" * 64,
                    "goal_contract": {"work_mode": "delivery"}},
            policy=SimpleNamespace(goal_contract_hash="a" * 64),
            kernel=("owl_open", "ward_enforce")),
        attempts=SimpleNamespace(controller=None),
        lifecycle_workflow=SimpleNamespace(
            lifecycle=lifecycle, active_lifecycle={"owl_open"}),
        behavior=SimpleNamespace(is_external=False),
    )
    monkeypatch.setattr(HOST_SESSION_REGISTRY, "resolve", lambda _token: session)

    trace = RuntimeTaskHandle("component-mode-trace").behavioral_trace_snapshot()

    assert trace["component_modes"] == {
        "owl": "passive", "ward": COMPONENT_BASELINE_MODES["ward"]}


def test_interceptor_module_is_a_compatibility_facade() -> None:
    assert CognitiveRuntime is RuntimeHostAPI
    assert CognitiveRuntime.__module__ == "runtime_host.host_api"
    assert callable(install_host_interceptor)
