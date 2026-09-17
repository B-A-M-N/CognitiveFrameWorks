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
import hashlib
import json
import os
import selectors
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class PersistentAgent:
    """One agent process owns one complete multi-turn trajectory."""

    def __init__(self, command: list[str]) -> None:
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        assert self.process.stdin is not None and self.process.stdout is not None
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.stdout, selectors.EVENT_READ)

    def request(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise RuntimeError(f"agent process exited with status {self.process.returncode}")
        self.stdin.write(json.dumps(request) + "\n")
        self.stdin.flush()
        if not self.selector.select(timeout):
            raise TimeoutError("agent command did not return a JSON response before timeout")
        line = self.stdout.readline()
        if not line:
            raise RuntimeError("agent command closed stdout without a JSON response")
        response = json.loads(line)
        if not isinstance(response, dict):
            raise ValueError("agent response must be a JSON object")
        return response

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.stdin.close()
        self.stdout.close()

    def __enter__(self) -> "PersistentAgent":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def execute_fixture_action(action: dict[str, Any], workspace: Path,
                           expected: str) -> dict[str, Any]:
    """Execute only the registered fixture binding and derive its oracle result."""
    binding = action.get("tool_binding_id") or action.get("tool")
    arguments = action.get("arguments") or {}
    raw_target = arguments.get("target")
    if not isinstance(raw_target, str) or not raw_target:
        return {"status": "fail", "fresh": True, "relevant": True,
                "reason": "missing fixture target"}
    target = Path(raw_target)
    if not target.is_absolute():
        target = workspace / target
    target = target.resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        return {"status": "fail", "fresh": True, "relevant": True,
                "reason": "fixture target outside workspace"}
    if binding != "write":
        return {"status": "fail", "fresh": True, "relevant": True,
                "reason": "fixture only registers write"}
    content = arguments.get("content")
    if not isinstance(content, str):
        return {"status": "fail", "fresh": True, "relevant": True,
                "reason": "write content is not text"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    actual = target.read_text(encoding="utf-8")
    return {"status": "pass" if actual == expected else "fail",
            "fresh": True, "relevant": True,
            "target": str(target),
            "content_digest": hashlib.sha256(actual.encode("utf-8")).hexdigest()}


def run_trial(api_module: Any, resolve: Any, command: list[str], *, task_id: str,
              profile_path: Path | None, max_turns: int, timeout: float,
              workspace: Path) -> dict[str, Any]:
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
    expected = "runtime-correct\n"
    fixture_target = workspace / "answer.txt"
    fixture_target.write_text("initial\n", encoding="utf-8")
    previous: list[dict[str, Any]] = []
    last_invocation: str | None = None
    last_result: dict[str, Any] | None = None
    metrics = {"task_id": task_id, "turns": 0, "actions": 0,
               "blocked_actions": 0, "prompt_words": 0, "finished": False}
    with PersistentAgent(command) as agent_process:
        for turn in range(max_turns):
            prompt = agent.before_model_call(tool_capable=True)
            metrics["turns"] += 1
            metrics["prompt_words"] += len(prompt.split())
            # Cohort, profile status, and treatment labels are intentionally not
            # present.  The agent sees only the real model-facing context.
            response = agent_process.request({
                "task": {"task_id": task_id, "subject_ref": request.subject_ref,
                         "shape": request.shape, "domain_tags": list(request.domain_tags),
                         "instruction": "Write runtime-correct\n to the fixture target, then complete.",
                         "fixture_target": str(fixture_target)},
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
                    result = execute_fixture_action(action, workspace, expected)
                    host.after_tool(tool_type=str(binding), result_class=result["status"],
                                    subject_ref=request.subject_ref,
                                    invocation_id=invocation_id, actual_result=result)
                    last_invocation, last_result = invocation_id, result
                    previous.append({"action": action, "decision": decision.value,
                                     "result": {"status": result["status"]}})
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
                workspace = Path(state) / "workspaces" / cohort / str(index)
                workspace.mkdir(parents=True, exist_ok=True)
                results[cohort].append(run_trial(
                    api_module, resolve, command,
                    task_id=f"real-agent-{cohort}-{index}", profile_path=profile,
                    max_turns=args.max_turns, timeout=args.timeout,
                    workspace=workspace))
        print(json.dumps({"experiment": "real-agent-control-treatment",
                          "cohort_labels_hidden_from_agent": True,
                          "results": results}, indent=2))
        return 0 if all(item["finished"] for group in results.values() for item in group) else 1


if __name__ == "__main__":
    raise SystemExit(main())
