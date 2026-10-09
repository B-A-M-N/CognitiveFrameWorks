#!/usr/bin/env python3
"""Codex adapter for host-controlled real-agent cohort trials.

The benchmark sends one JSON request per model turn. This process owns the
runtime for that trial, injects treatment context into each Codex prompt, and
executes proposed read/inspect/write actions itself. Codex runs read-only with
user MCP configuration disabled; the private DP control MCP belongs only to
the separate trusted host process.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cohort.runtime_harness import CohortTrialHost, HarnessError, InvalidTreatment
from cohort.agent_codex import CODEX_TIMEOUT_SECONDS, _fields_for, parse_codex_jsonl

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "properties": {
                "tool_binding_id": {"type": "string", "enum": ["read", "inspect", "write"]},
                # Keep the wire schema deliberately flat: Codex strict JSON
                # schema rejects task-dependent/open-ended nested objects.
                # The trusted host parses and validates this JSON payload.
                "arguments": {"type": "string"},
            }, "required": ["tool_binding_id", "arguments"], "additionalProperties": False,
        }},
        "answer": {"type": "string"},
        "complete": {"type": "boolean"},
    },
    "required": ["actions", "answer", "complete"],
    "additionalProperties": False,
}


def _codex_command(workspace: Path, schema_path: Path) -> list[str]:
    return [
        "codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral",
        "--ignore-user-config", "--color", "never", "--sandbox", "read-only",
        "--cd", str(workspace), "--output-schema", str(schema_path),
    ]


def _codex_environment(source: Mapping[str, str] | None = None, *,
                      writable_home: Path | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    env.pop("CODEX_SANDBOX", None)
    if writable_home is not None:
        original_home = env.get("HOME")
        if not env.get("CODEX_HOME") and original_home:
            env["CODEX_HOME"] = str(Path(original_home) / ".codex")
        env["HOME"] = str(writable_home)
    # Runtime and provider configuration is passed to the private DP child by
    # MCPControlClient. Keep it out of the model subprocess environment. In
    # particular, the OpenRouter credential used to launch FreeCodex must not
    # become visible to a model-issued shell command in this separate process.
    # API/provider credentials are also removed so this path uses the normal
    # authenticated Codex session rather than silently switching to API billing.
    for key in tuple(env):
        upper_key = key.upper()
        if (key.startswith(("DIGITALPSYCHOLOGY_", "COGNITIVE_"))
                or key in {"PYTHONPATH", "PYTHONHOME"}
                or upper_key.endswith(("_API_KEY", "_TOKEN", "_SECRET",
                                       "_PASSWORD", "_ACCESS_KEY", "_PRIVATE_KEY"))):
            env.pop(key, None)
    return env


def _previous_turns(previous: Any) -> list[dict[str, Any]]:
    if not isinstance(previous, list):
        return []
    results = []
    for row in previous[-12:]:
        response = row.get("response") if isinstance(row, Mapping) else None
        if not isinstance(response, Mapping):
            continue
        result = {key: response.get(key) for key in (
            "action_results", "host_action_receipts", "treatment_exposure")
                  if response.get(key) is not None}
        if result:
            results.append(result)
    return results


def _codex_jsonl_diagnostics(stdout: str) -> dict[str, Any]:
    """Summarize protocol shape without retaining prompt or response contents."""
    event_types: list[str] = []
    item_types: list[str] = []
    malformed_lines = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed_lines += 1
            continue
        if not isinstance(event, Mapping):
            malformed_lines += 1
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and len(event_types) < 32:
            event_types.append(event_type[:64])
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, Mapping) else None
        if isinstance(item_type, str) and len(item_types) < 32:
            item_types.append(item_type[:64])
    return {
        "jsonl_event_types": event_types,
        "jsonl_item_types": item_types,
        "malformed_jsonl_lines": malformed_lines,
        "final_agent_message_present": "agent_message" in item_types,
    }


def _bounded_stderr_excerpt(stderr: str) -> str:
    text = " ".join(stderr.split())
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+",
        r"\1[redacted]", text)
    return text[-512:]


class CodexAdapterFailure(RuntimeError):
    """Bounded Codex process/protocol failure with non-content diagnostics."""

    def __init__(self, message: str, diagnostics: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.codex_diagnostics = dict(diagnostics)


def _prompt(task: Mapping[str, Any], context: str, previous: list[dict[str, Any]]) -> str:
    instruction = str(task.get("instruction") or "")
    workspace = str(task.get("workspace") or "")
    field_spec = _fields_for(task)
    return f"""You are completing a bounded benchmark task. The trusted host executes every task action.

TASK FAMILY: {task.get('task_family', '')}
TASK INSTRUCTION:
{instruction}

TRIAL WORKSPACE: {workspace}

HOST-PRODUCED RUNTIME CONTEXT (trusted task guidance):
{context}

PRIOR HOST ACTION RESULTS:
{json.dumps(previous, sort_keys=True, separators=(',', ':'))}

Use only the host action protocol. Do not use shell, direct file tools, MCP,
network, or any other tool to perform task actions. Propose actions in the
`actions` array; the host executes them and sends the results on the next turn.
Only these tools exist: read(target), inspect(target, operation='list'),
write(target, content). Paths are relative to the trial workspace. A correct
answer requires at least one executed host action. Do not request destructive,
network, or shell actions.

Return exactly the schema-required JSON object. Encode each action's
`arguments` as a JSON string containing its object (for example,
`{{"target":"README.md"}}`). Encode `answer` as a JSON string containing
an object with all these evaluator fields: {field_spec}. Set `complete` true
when you have proposed all actions needed for the task. You may set it true in
the same response as your final action: the trusted host accepts completion
only after it confirms every proposed action executed, and asks for another
turn if any action fails. Base your answer on evidence already returned by the
host, or on the task instruction when no read is needed. Never fabricate a
host action result.
"""


def _load_reply(message: str) -> dict[str, Any]:
    parsed = json.loads(message)
    if not isinstance(parsed, dict):
        raise ValueError("Codex final message must be a JSON object")
    if (set(parsed) != {"actions", "answer", "complete"}
            or not isinstance(parsed.get("actions"), list)
            or not isinstance(parsed.get("answer"), (dict, str))
            or type(parsed.get("complete")) is not bool):
        raise ValueError("Codex final message does not match the host action schema")

    answer = parsed["answer"]
    if isinstance(answer, str):
        answer = json.loads(answer)
    if not isinstance(answer, dict):
        raise ValueError("Codex answer JSON string must contain an object")

    actions = []
    for action in parsed["actions"]:
        if not isinstance(action, dict):
            raise ValueError("Codex actions must contain objects")
        arguments = action.get("arguments")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Codex action arguments must contain an object")
        actions.append({**action, "arguments": arguments})
    parsed["actions"] = actions
    parsed["answer"] = answer
    return parsed


async def _process_turn(host: CohortTrialHost, request: Mapping[str, Any],
                        schema_path: Path) -> dict[str, Any]:
    task = request.get("task") or {}
    if not isinstance(task, Mapping):
        raise ValueError("benchmark task must be an object")
    turn = request.get("turn", 0)
    if type(turn) is not int or turn < 0:
        raise ValueError("benchmark turn must be a non-negative integer")
    context = await host.context_for_turn()
    workspace = Path(str(task.get("workspace") or "")).resolve()
    if workspace != host.workspace:
        raise HarnessError("benchmark workspace changed within a treatment trial")
    previous = _previous_turns(request.get("previous"))
    codex_home = schema_path.parent / f"codex-home-turn-{turn}"
    codex_home.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        _codex_command(workspace, schema_path), input=_prompt(task, context, previous),
        capture_output=True, text=True, timeout=CODEX_TIMEOUT_SECONDS,
        cwd=workspace, env=_codex_environment(writable_home=codex_home), check=False)
    codex_diagnostics = _codex_jsonl_diagnostics(proc.stdout)
    if proc.returncode != 0:
        codex_diagnostics.update({
            "process_exit_code": proc.returncode,
            "stderr_excerpt": _bounded_stderr_excerpt(proc.stderr),
        })
        raise CodexAdapterFailure(
            f"codex exec exited with status {proc.returncode}", codex_diagnostics)
    try:
        message, usage = parse_codex_jsonl(proc.stdout)
    except Exception as exc:
        codex_diagnostics["parse_error_class"] = type(exc).__name__
        raise CodexAdapterFailure(
            "Codex returned an invalid JSONL response", codex_diagnostics) from exc
    try:
        proposed = _load_reply(message)
    except Exception as exc:
        codex_diagnostics["output_parse_error_class"] = type(exc).__name__
        raise CodexAdapterFailure(
            "Codex final message did not match the host action schema",
            codex_diagnostics) from exc

    action_results: list[dict[str, Any]] = []
    turn_receipts: list[dict[str, Any]] = []
    for index, action in enumerate(proposed["actions"]):
        if index >= 8:
            raise HarnessError("host action budget exceeded for this model turn")
        outcome = await host.execute_action(action, turn=turn, action_number=index)
        action_results.append({"authorized": outcome["authorized"],
                               "output": outcome["output"]})
        turn_receipts.append(outcome["receipt"])

    dp_behavioral_record = await host.behavioral_record(trial_id=host.task_id)

    complete = proposed["complete"] is True
    if complete and not any(item.get("executed") is True for item in turn_receipts):
        complete = False
    if any(item.get("executed") is not True for item in turn_receipts):
        complete = False
    exposure = {
        "configuration": host.configuration,
        "frameworks": list(host.exposure.get("frameworks") or ()),
        "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "context_bytes": len(context.encode("utf-8")),
        "host_generated": True,
        "turn": turn,
        "private_dp_mcp": bool(host.exposure.get("private_dp_mcp", False)),
        "dp_behavior_revision": (int(host.bridge._state.revision)
                                  if host.bridge is not None else None),
        "csw_authority_id": host.exposure.get("csw_authority_id"),
        "csw_registry_hash": host.exposure.get("csw_registry_hash"),
        "semantic_provider_attestation": host.exposure.get(
            "semantic_provider_attestation"),
    }
    return {
        "answer": json.dumps(proposed["answer"], sort_keys=True),
        "complete": complete,
        "usage": usage,
        "usage_source": "codex.exec.turn.completed",
        "codex_diagnostics": {
            **codex_diagnostics,
            "action_proposal_count": len(proposed["actions"]),
            "host_action_receipt_count": len(turn_receipts),
        },
        "treatment_exposure": exposure,
        "action_results": action_results,
        "host_action_receipts": turn_receipts,
        "host_receipt_count": len(turn_receipts),
        "dp_behavioral_record": dp_behavioral_record,
        "tool_visibility_evidence": {
            "private_dp_mcp": "host-only-private-stdio",
            "dp_control_tools_model_visible": False,
            "mcp_servers": {"status": "disabled", "names": [],
                            "config_override": "--ignore-user-config"},
            "full_agent_tool_registry": {
                "status": "missing",
                "reason": "codex exec JSONL does not expose the complete live tool registry",
            },
        },
    }


async def _serve(*, configuration: str, raw_state: Path,
                 schema_path: Path) -> None:
    host: CohortTrialHost | None = None
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            task = request.get("task") if isinstance(request, dict) else None
            if not isinstance(task, dict):
                raise ValueError("benchmark request has no task object")
            if host is None:
                host = CohortTrialHost(
                    configuration=configuration, task=task,
                    state_root=raw_state / "runtime",
                    workspace=Path(str(task.get("workspace") or "")))
                await host.open()
            payload = await _process_turn(host, request, schema_path)
        except InvalidTreatment as exc:
            payload = {"answer": json.dumps({"status": "blocked", "reason": str(exc)}),
                       "complete": True, "infrastructure_failure": True,
                       "usage_source": "missing",
                       "treatment_exposure": {"configuration": configuration,
                                               "host_generated": False},
                       "host_action_receipts": []}
        except Exception as exc:
            payload = {"answer": json.dumps({"status": "error",
                                              "reason": f"{type(exc).__name__}: {exc}"[:240]}),
                       "complete": True, "infrastructure_failure": True,
                       "usage_source": "missing",
                       "treatment_exposure": {"configuration": configuration,
                                               "host_generated": False},
                       "host_action_receipts": []}
            diagnostics = getattr(exc, "codex_diagnostics", None)
            if isinstance(diagnostics, Mapping):
                payload["codex_diagnostics"] = dict(diagnostics)
        print(json.dumps(payload, sort_keys=True), flush=True)
    if host is not None:
        await host.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", default="baseline")
    parser.add_argument("--state-root", type=Path)
    args = parser.parse_args(argv)
    owned_tmp = None
    raw_state = args.state_root
    if raw_state is None:
        owned_tmp = tempfile.TemporaryDirectory(prefix="cohort-host-state-")
        raw_state = Path(owned_tmp.name)
    raw_state.mkdir(parents=True, exist_ok=True)
    schema_path = raw_state / "codex-host-output-schema.json"
    schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")
    try:
        import asyncio
        asyncio.run(_serve(configuration=args.configuration, raw_state=raw_state,
                           schema_path=schema_path))
    finally:
        if owned_tmp is not None:
            owned_tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
