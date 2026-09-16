#!/usr/bin/env python3
"""Run a real external agent/model loop through Cognitive Runtime.

The command supplied with ``--agent-command`` is the agent under test.  It
receives one JSON request per model turn on stdin and must return one JSON
object on stdout.  The cohort is deliberately omitted from that request.

Response shape::

    {"action": {"tool_binding_id": "test", "arguments": {...}},
     "complete": false}

The host owns tool execution, evidence, completion, and telemetry.  This
harness is intentionally not a fake-agent acceptance suite; without an
explicit command it refuses to run.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def invoke(command: list[str], request: dict[str, Any], timeout: float) -> dict[str, Any]:
    completed = subprocess.run(
        command, input=json.dumps(request) + "\n", text=True,
        capture_output=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"agent command failed ({completed.returncode}): {completed.stderr[-1000:]}")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("agent command returned no JSON response")
    response = json.loads(lines[-1])
    if not isinstance(response, dict):
        raise ValueError("agent response must be a JSON object")
    return response


def run_trial(api_module: Any, resolve: Any, command: list[str], *, task_id: str,
              profile_path: Path | None, max_turns: int, timeout: float) -> dict[str, Any]:
    api = api_module.CognitiveRuntime(
        routing_profile_path=str(profile_path) if profile_path else None)
    request = resolve.TaskRequest(
        task_id=task_id, subject_ref=f"subject:{task_id}", shape="performance",
        model="external-model", harness="external-agent-harness",
        toolset="fixture", agent_id="external-agent",
        agent_instance_id="external-agent-instance")
    session = api.begin_task(request)
    agent = api.agent_handle(session)
    host = api.host_handle(session)
    previous: list[dict[str, Any]] = []
    last_invocation: str | None = None
    last_result: dict[str, Any] | None = None
    metrics = {"task_id": task_id, "turns": 0, "actions": 0,
               "blocked_actions": 0, "prompt_words": 0, "finished": False}
    for turn in range(max_turns):
        prompt = agent.before_model_call(tool_capable=True)
        metrics["turns"] += 1
        metrics["prompt_words"] += len(prompt.split())
        # Cohort, profile status, and treatment labels are intentionally not
        # present.  The agent sees only the real model-facing context.
        response = invoke(command, {
            "task": {"task_id": task_id, "subject_ref": request.subject_ref,
                     "shape": request.shape, "domain_tags": list(request.domain_tags)},
            "prompt": prompt, "turn": turn, "previous_results": previous,
        }, timeout)
        action = response.get("action")
        if action is not None:
            if not isinstance(action, dict):
                raise ValueError("agent action must be a JSON object")
            action = dict(action)
            action.setdefault("subject_ref", request.subject_ref)
            decision = agent.request_action(action)
            metrics["actions"] += 1
            if decision.value != "allow":
                metrics["blocked_actions"] += 1
                previous.append({"action": action, "decision": decision.value})
            else:
                binding = action.get("tool_binding_id") or action.get("tool")
                invocation_id = f"agent-{task_id}-{turn}"
                result = {"status": "pass", "tool_binding_id": binding,
                          "target": action.get("arguments", {}).get("target", "task-output")}
                host.after_tool(tool_type=str(binding), result_class="pass",
                                subject_ref=request.subject_ref,
                                invocation_id=invocation_id, actual_result=result)
                last_invocation, last_result = invocation_id, result
                previous.append({"action": action, "decision": decision.value,
                                 "result": {"status": "pass"}})
        if response.get("complete"):
            if last_invocation is None or last_result is None:
                previous.append({"completion": "blocked_no_host_result"})
                continue
            completion = host.validate_completion_boundary(
                subject_ref=request.subject_ref, validator_id="completion-boundary-v1",
                invocation_id=last_invocation, result=last_result)
            if completion.value != "allow":
                previous.append({"completion": completion.value})
                continue
            host.finish_task({"status": "completed", "task_id": task_id})
            metrics["finished"] = True
            break
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-command", required=True,
                        help="external model/agent command; one JSON request in, one JSON response out")
    parser.add_argument("--treatment-profile", type=Path, required=True,
                        help="validated/active DP BehavioralRoutingProfile JSON")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if args.trials < 1 or args.max_turns < 1:
        raise SystemExit("trials and max-turns must be positive")
    command = shlex.split(args.agent_command)
    if not command:
        raise SystemExit("agent command is empty")
    if not args.treatment_profile.exists():
        raise SystemExit(f"treatment profile does not exist: {args.treatment_profile}")

    with tempfile.TemporaryDirectory(prefix="cfw-real-agent-") as state:
        os.environ["XDG_RUNTIME_DIR"] = state
        import importlib.util
        def load(name: str, path: Path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        resolve = load("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
        load("runtime", ROOT / "scripts" / "runtime.py")
        api_module = load("cognitive_runtime", ROOT / "scripts" / "cognitive_runtime.py")
        results = {"control": [], "treatment": []}
        for cohort, profile in (("control", None), ("treatment", args.treatment_profile)):
            for index in range(args.trials):
                results[cohort].append(run_trial(
                    api_module, resolve, command,
                    task_id=f"real-agent-{cohort}-{index}", profile_path=profile,
                    max_turns=args.max_turns, timeout=args.timeout))
        print(json.dumps({"experiment": "real-agent-control-treatment",
                          "cohort_labels_hidden_from_agent": True,
                          "results": results}, indent=2))
        return 0 if all(item["finished"] for group in results.values() for item in group) else 1


if __name__ == "__main__":
    raise SystemExit(main())
