"""Local integration checks for host-produced cohort evidence.

These tests run the real CFW host path and benchmark evaluator, but replace the
Codex subprocess with a JSONL fixture so no model inference is performed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _load(
    "real_agent_cohort_runtime_benchmark",
    ROOT / "scripts" / "acceptance-real-agent-benchmark.py")
codex_host = _load(
    "real_agent_cohort_runtime_codex_host",
    ROOT / "scripts" / "cohort" / "agent_codex_host.py")


def test_codex_action_schema_uses_closed_string_payloads() -> None:
    action_schema = codex_host._OUTPUT_SCHEMA["properties"]["actions"]["items"]
    assert action_schema["additionalProperties"] is False
    assert action_schema["properties"]["arguments"] == {"type": "string"}
    assert codex_host._OUTPUT_SCHEMA["properties"]["answer"] == {"type": "string"}


def test_codex_reply_parses_json_string_payloads() -> None:
    result = codex_host._load_reply(json.dumps({
        "actions": [{"tool_binding_id": "read",
                     "arguments": "{\"target\":\"README.md\"}"}],
        "answer": "{\"status\":\"checked\"}",
        "complete": True,
    }))

    assert result["actions"][0]["arguments"] == {"target": "README.md"}
    assert result["answer"] == {"status": "checked"}


def test_final_action_can_complete_under_host_execution_gate() -> None:
    prompt = codex_host._prompt(
        {"task_family": "code_repair", "instruction": "Write fixed.py.",
         "workspace": "/tmp/benchmark-workspace"},
        "Host confirms writes.", [])
    prompt = " ".join(prompt.split())

    assert "same response as your final action" in prompt
    assert "accepts completion only after it confirms every proposed action executed" in prompt
    assert "Set `complete` true only after the host has returned" not in prompt


def test_model_subprocess_does_not_inherit_private_runtime_or_openrouter_keys(
        monkeypatch) -> None:
    monkeypatch.setenv("DIGITALPSYCHOLOGY_STATE_ROOT", "/private/dp-state")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-model-shell")
    monkeypatch.setenv("COHORT_RUNTIME_SETTING", "non-secret")

    env = codex_host._codex_environment()

    assert "DIGITALPSYCHOLOGY_STATE_ROOT" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert env["COHORT_RUNTIME_SETTING"] == "non-secret"


@pytest.mark.parametrize(
    ("configuration", "expected_frameworks"),
    [("cfw", ["CFW"]), ("cfw_csw_dp", ["CFW", "CSW", "DP"])],
)
def test_treatment_context_and_host_action_receipts_reach_evaluator(
        tmp_path: Path, monkeypatch, configuration: str,
        expected_frameworks: list[str]) -> None:
    contract = json.loads(
        (ROOT / "contracts" / "real-agent-benchmark-v1.json").read_text())
    contract["task_families"] = [
        family for family in contract["task_families"]
        if family["id"] == "code_repair"]
    captured: dict = {}
    original_load_task = benchmark.load_task_module

    def load_task(path: Path):
        module = original_load_task(path)
        original_evaluate = module.evaluate

        def evaluate(evidence, workspace):
            captured.update(evidence)
            return original_evaluate(evidence, workspace)

        module.evaluate = evaluate
        return module

    monkeypatch.setattr(benchmark, "load_task_module", load_task)

    jsonl = "\n".join(json.dumps(event) for event in (
        {"type": "item.completed", "item": {
            "id": "agent-message", "type": "agent_message", "text": json.dumps({
                "actions": [{
                    "tool_binding_id": "write",
                    "arguments": {
                        "target": "fixed.py",
                        "content": "def add(a, b):\n    return a + b\n",
                    },
                }],
                "answer": {"status": "fixed", "reason": "wrote the repair",
                           "files_changed": 1},
                "complete": True,
            })}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 11, "output_tokens": 7}},
    ))

    def fake_codex_run(command, **kwargs):
        return codex_host.subprocess.CompletedProcess(
            command, 0, stdout=jsonl, stderr="")

    monkeypatch.setattr(codex_host.subprocess, "run", fake_codex_run)
    monkeypatch.setenv("DIGITALPSYCHOLOGY_SYSTEM_ONE_MODE", "disabled")
    monkeypatch.setenv("DIGITALPSYCHOLOGY_SEMANTIC_PROVIDER", "disabled")
    state_root = tmp_path / "cfw-host-state"

    def host_agent(command, task, **_limits):
        from cohort.runtime_harness import CohortTrialHost

        async def run_turn():
            host = CohortTrialHost(
                configuration=configuration, task=task,
                state_root=state_root,
                workspace=Path(task["workspace"]))
            await host.open()
            schema = state_root / "schema.json"
            state_root.mkdir(parents=True, exist_ok=True)
            schema.write_text(json.dumps(codex_host._OUTPUT_SCHEMA))
            request = {"task": task, "turn": 0, "previous": []}
            try:
                response = await codex_host._process_turn(host, request, schema)
                return [{"turn": 0, "response": response}], {
                    "model_calls": 1,
                    "external_tokens": (response["usage"]["input_tokens"]
                                        + response["usage"]["output_tokens"]),
                    "monetary_cost": 0.0,
                }
            finally:
                await host.close()

        return asyncio.run(run_turn())

    monkeypatch.setattr(benchmark, "run_agent", host_agent)
    report = benchmark.execute_benchmark(
        contract, ["fixture-host"], trials_per_cell=1, holdout_fraction=0.0)

    row = report["results"][0]
    assert row["completion_success"] is True, json.dumps(row, sort_keys=True)
    assert captured["task"]["task_family"] == "code_repair"
    exposure = captured["treatment_exposure"][0]
    assert exposure["configuration"] == configuration
    assert exposure["frameworks"] == expected_frameworks
    assert exposure["host_generated"] is True
    assert exposure["context_bytes"] > 0
    assert len(exposure["context_sha256"]) == 64
    if "DP" in expected_frameworks:
        assert exposure["private_dp_mcp"] is True
        assert exposure["csw_authority_id"]
        assert exposure["csw_registry_hash"]
    receipts = captured["host_action_receipts"]
    assert len(receipts) == 1
    assert receipts[0]["executed"] is True
    assert receipts[0]["tool"] == "write"
    assert receipts[0]["result"]["operation"] == "write"
    if "DP" in expected_frameworks:
        assert receipts[0]["cfw_gate"]["action_authorized"] is True
        assert receipts[0]["dp_committed_boundary"]["status"] == "ready"
        assert any(gate["stage"] == "final" and gate["decision"] == "allow"
                   for gate in receipts[0]["cfw_gate_receipts"])
        assert exposure["csw_authority_id"] == receipts[0]["csw_authority_id"]
        assert len(captured["runtime_behavioral_telemetry"]) == 1
        assert captured["runtime_behavioral_telemetry"][0]["control_transition_count"] > 0
    else:
        assert receipts[0]["cfw_gate"]["decision"] == "allow"
    assert row["host_action_receipts"] == receipts
    assert row["treatment_exposure"] == captured["treatment_exposure"]
    assert row["runtime_behavioral_telemetry"] == captured["runtime_behavioral_telemetry"]
    assert row["token_overhead"] == 18


def test_laya_arm_fails_closed_without_live_private_mcp_attestation(
        tmp_path: Path, monkeypatch) -> None:
    from cohort.runtime_harness import CohortTrialHost, InvalidTreatment

    monkeypatch.setenv("DIGITALPSYCHOLOGY_SYSTEM_ONE_MODE", "disabled")
    monkeypatch.setenv("DIGITALPSYCHOLOGY_SEMANTIC_PROVIDER", "disabled")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = CohortTrialHost(
        configuration="cfw_csw_dp_laya",
        task={"task_id": "laya-no-handshake", "task_family": "test",
              "instruction": "confirm the host refuses unattested Laya"},
        state_root=tmp_path / "host-state", workspace=workspace)
    async def check_handshake():
        with pytest.raises(
                InvalidTreatment, match="live pinned Laya capability handshake"):
            await host.open()
        await host.close()

    asyncio.run(check_handshake())
