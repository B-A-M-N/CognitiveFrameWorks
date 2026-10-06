from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "acceptance-real-agent.py"
spec = importlib.util.spec_from_file_location("acceptance_real_agent_bounds", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

AGENT_MODULE_PATH = ROOT / "scripts" / "cohort" / "agent_codex.py"
agent_spec = importlib.util.spec_from_file_location(
    "cohort_agent_codex_bounds", AGENT_MODULE_PATH)
agent_codex = importlib.util.module_from_spec(agent_spec)
sys.modules[agent_spec.name] = agent_codex
agent_spec.loader.exec_module(agent_codex)


def test_budget_stops_model_calls_tokens_cost_and_time() -> None:
    budget = module.QualificationBudget(
        max_model_calls=2, max_elapsed_seconds=1,
        max_external_tokens=10, max_monetary_cost=2,
    )
    budget.record_response({"usage": {"input_tokens": 2, "output_tokens": 3, "cost": 0.5}})
    budget.check()
    budget.record_response({"usage": {"input_tokens": 2, "output_tokens": 3, "cost": 0.5}})
    try:
        budget.check()
    except RuntimeError as exc:
        assert "model-call" in str(exc)
    else:
        raise AssertionError("model-call budget was not enforced")
    token_bounded = module.QualificationBudget(
        max_model_calls=10, max_elapsed_seconds=10,
        max_external_tokens=4, max_monetary_cost=2,
    )
    token_bounded.record_response({"usage": {"input_tokens": 3, "output_tokens": 2}})
    try:
        token_bounded.check()
    except RuntimeError as exc:
        assert "external-token" in str(exc)
    else:
        raise AssertionError("external-token budget was not enforced")
    cost_bounded = module.QualificationBudget(
        max_model_calls=10, max_elapsed_seconds=10,
        max_external_tokens=10, max_monetary_cost=0.5,
    )
    cost_bounded.record_response({"usage": {"cost": 0.5}})
    try:
        cost_bounded.check()
    except RuntimeError as exc:
        assert "monetary-cost" in str(exc)
    else:
        raise AssertionError("monetary-cost budget was not enforced")
    bounded = module.QualificationBudget(
        max_model_calls=10, max_elapsed_seconds=0.001,
        max_external_tokens=10, max_monetary_cost=2,
    )
    time.sleep(0.002)
    try:
        bounded.check()
    except RuntimeError as exc:
        assert "elapsed" in str(exc)
    else:
        raise AssertionError("elapsed budget was not enforced")


def test_codex_adapter_uses_sandbox_mcp_off_and_jsonl_usage(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "trial-workspace"
    workspace.mkdir()
    assert not (workspace / ".codex" / "config.toml").exists()
    claimed_usage = {"input_tokens": 9000, "output_tokens": 8000, "cost": 99.0}
    message = json.dumps({
        "answer": json.dumps({
            "status": "fixed", "reason": "updated the requested file",
            "files_changed": 1,
        }),
        "complete": True,
        "usage": claimed_usage,
    })
    events = "\n".join(json.dumps(event) for event in (
        {"type": "thread.started", "thread_id": "fixture-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "assistant-message", "type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 37, "cached_input_tokens": 12,
            "output_tokens": 19, "total_tokens": 56}},
    ))
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return agent_codex.subprocess.CompletedProcess(
            command, 0, stdout=events, stderr="")

    monkeypatch.setattr(agent_codex.subprocess, "run", fake_run)
    monkeypatch.setenv("CODEX_SANDBOX", "danger-full-access")
    monkeypatch.setattr(agent_codex.sys, "stdin", io.StringIO(json.dumps({
        "task": {
            "task_family": "code_repair", "instruction": "Repair the fixture.",
            "workspace": str(workspace),
        },
        "turn": 0,
        "previous": [],
    }) + "\n"))
    stdout = io.StringIO()
    monkeypatch.setattr(agent_codex.sys, "stdout", stdout)

    assert agent_codex.main() == 0
    response = json.loads(stdout.getvalue())
    command = captured["command"]
    kwargs = captured["kwargs"]
    assert "--json" in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--cd") + 1] == str(workspace.resolve())
    assert "--ignore-user-config" in command
    assert "mcp_servers={}" not in command
    assert "danger-full-access" not in command
    assert kwargs["cwd"] == str(workspace.resolve())
    assert kwargs["timeout"] == agent_codex.CODEX_TIMEOUT_SECONDS
    assert "CODEX_SANDBOX" not in kwargs["env"]
    assert response["usage"] == {"input_tokens": 37, "output_tokens": 19}
    assert response["usage_source"] == "codex.exec.turn.completed"
    assert response["answer"] == json.dumps({
        "status": "fixed", "reason": "updated the requested file",
        "files_changed": 1,
    })
    assert response["tool_visibility_evidence"]["mcp_servers"] == {
        "status": "disabled", "names": [],
        "config_override": "--ignore-user-config"
    }
    assert response["tool_visibility_evidence"]["full_agent_tool_registry"][
        "status"] == "missing"


def test_codex_jsonl_missing_actual_usage_fails_closed() -> None:
    events = "\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {
            "id": "assistant-message", "type": "agent_message", "text": "{}"}},
        {"type": "turn.completed", "usage": {"input_tokens": 2}},
    ))
    try:
        agent_codex.parse_codex_jsonl(events)
    except ValueError as exc:
        assert "output_tokens" in str(exc)
    else:
        raise AssertionError("missing Codex event usage was accepted")


def test_agent_tool_visibility_uses_a_fixed_model_request_without_dp_tools() -> None:
    request = module.model_visible_agent_request(
        task={"task_id": "visibility-check", "instruction": "Inspect and report."},
        prompt="Use the host provided action protocol.", turn=0, previous_results=[],
    )

    assert set(request) == module.MODEL_VISIBLE_REQUEST_KEYS
    serialized = __import__("json").dumps(request).lower()
    assert not any(tool in serialized for tool in module.FORBIDDEN_DP_AGENT_TOOLS)
    assert "tools" not in request
    assert "mcp_servers" not in request

    try:
        module.model_visible_agent_request(
            task={"task_id": "visibility-check", "tools": ["write"]},
            prompt="", turn=0, previous_results=[])
    except RuntimeError as exc:
        assert "tool registry" in str(exc)
    else:
        raise AssertionError("model-visible MCP registry was accepted")

    try:
        module.model_visible_agent_request(
            task={"task_id": "visibility-check"},
            prompt="prepare_control_tick", turn=0, previous_results=[])
    except RuntimeError as exc:
        assert "DP MCP control tool" in str(exc)
    else:
        raise AssertionError("DP control tool name reached the model request")


def test_persistent_agent_close_terminates_descendant_process_group() -> None:
    marker = Path(tempfile.mkdtemp()) / "child.pid"
    code = f'''import os, time
child = os.fork()
if child == 0:
    staging = {str(marker)!r} + ".partial"
    with open(staging, "w") as handle:
        handle.write(str(os.getpid()))
    os.replace(staging, {str(marker)!r})
    while True: time.sleep(1)
print(os.getpid(), flush=True)
'''
    agent = module.PersistentAgent([sys.executable, "-c", code])
    parent_pid = agent.process.pid
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                child_pid = int(marker.read_text().strip())
                break
            except (ValueError, OSError):
                pass
        time.sleep(0.01)
    else:
        raise AssertionError("forked descendant never published its pid")
    agent.close()
    deadline = time.monotonic() + 2
    def descendant_is_live() -> bool:
        stat_path = Path(f"/proc/{child_pid}/stat")
        if not stat_path.exists():
            return False
        try:
            fields = stat_path.read_text().split()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return len(fields) > 2 and fields[2] != "Z"
    while time.monotonic() < deadline and descendant_is_live():
        time.sleep(0.02)
    assert not descendant_is_live(), "agent descendant survived process-group cleanup"
