from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_host.handles import (AgentSessionHandle, HostSessionHandle,
                                  RuntimeTaskHandle)


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


def test_host_handle_accepts_the_runtime_host_api_explicitly() -> None:
    host_api = object()
    handle = HostSessionHandle("unresolved-test-token", host_api=host_api)

    assert handle._host_api() is host_api
