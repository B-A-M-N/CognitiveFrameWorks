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
DP_ROOT = ROOT.parent / "DigitalPsychology"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(DP_ROOT))


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
              workspace: Path, experiment_plan: dict[str, Any] | None = None,
              agent_instance_id: str = "external-agent-instance") -> dict[str, Any]:
    api = api_module.CognitiveRuntime(
        routing_profile_path=str(profile_path) if profile_path else None)
    request = resolve.TaskRequest(
        task_id=task_id, subject_ref=f"subject:{task_id}", shape="performance",
        model="external-model", harness="external-agent-harness",
        toolset="fixture", agent_id="external-agent",
        agent_instance_id=agent_instance_id,
        routing_experiment_plan=experiment_plan or {})
    session = api.begin_task(request)
    agent = api.agent_handle(session)
    host = api.host_handle(session)
    expected = "runtime-correct\n"
    fixture_target = workspace / "answer.txt"
    fixture_target.write_text("initial\n", encoding="utf-8")
    previous: list[dict[str, Any]] = []
    last_invocation: str | None = None
    last_result: dict[str, Any] | None = None
    route_evidence_ref: str | None = None
    metrics = {"task_id": task_id, "turns": 0, "actions": 0,
               "blocked_actions": 0, "prompt_words": 0, "finished": False,
               "oracle_success": False, "route_outcome": None,
               "session_id": session.session_id, "attempt_id": session.attempt_id,
               "cohort": ((session.bundle.guard_pack.get("routing_experiment") or {})
                          .get("routing_cohort")),
               "candidate_profile_hash": ((session.bundle.guard_pack.get("routing_experiment") or {})
                                           .get("candidate_profile_hash"))}
    route_decision_id: str | None = None
    if experiment_plan:
        adjustment = experiment_plan.get("candidate_adjustment") or {}
        route_decision_id = next((decision_id for decision_id, decision in session.route_decisions.items()
                                  if decision.get("route") == adjustment.get("route")
                                  and decision.get("route_type") == adjustment.get("route_type")), None)
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
                    if experiment_plan and route_decision_id:
                        try:
                            evaluator_evidence = host.after_validator(
                                validator_id="reinspection-v1", invocation_id=invocation_id,
                                result=result, subject_ref=request.subject_ref)
                            # Route effectiveness is scored on the final
                            # host-validated artifact for the trajectory. A
                            # prior intermediate success must not make a
                            # later failed final state look successful.
                            route_evidence_ref = evaluator_evidence
                        except (ValueError, PermissionError) as exc:
                            previous.append({"route_outcome": f"blocked:{exc}"})
                    # A completed tool result is an artifact boundary.  The
                    # host, not the model, decides which lifecycle stages are
                    # active for the next immutable model subcall.
                    host.on_artifact(flow_triggered=True)
                    previous.append({"action": action, "decision": decision.value,
                                     "result": {"status": result["status"]}})
            if response.get("complete"):
                if last_invocation is None or last_result is None:
                    previous.append({"completion": "blocked_no_host_result"})
                    continue
                try:
                    completion = host.validate_completion_boundary(
                        subject_ref=request.subject_ref, validator_id="completion-boundary-v1",
                        invocation_id=last_invocation, result=last_result)
                except (ValueError, PermissionError) as exc:
                    # A real agent may claim completion against a failed
                    # artifact.  The host records the failed attempt and
                    # keeps the trajectory alive; it must not turn an
                    # ordinary failed trial into a harness crash.
                    previous.append({"completion": f"blocked:{exc}"})
                    continue
                if completion.value != "allow":
                    previous.append({"completion": completion.value})
                    continue
                host.finish_task({"status": "completed", "task_id": task_id})
                metrics["finished"] = True
                metrics["oracle_success"] = bool(last_result.get("status") == "pass")
                break
    if experiment_plan and route_decision_id and metrics["route_outcome"] is None:
        metrics["route_outcome"] = host.evaluate_route_outcome(
            route_decision_id=route_decision_id,
            evaluator_id=experiment_plan["target_evaluator"],
            evidence_refs=[route_evidence_ref] if route_evidence_ref else [])
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-command", required=True,
                        help="external model/agent command; one JSON request in, one JSON response out")
    parser.add_argument("--treatment-profile", type=Path,
                        help="validated/active DP BehavioralRoutingProfile JSON (legacy replay mode)")
    parser.add_argument("--experiment-plan", type=Path,
                        help="predeclared RoutingExperimentPlan JSON (closed-loop mode)")
    parser.add_argument("--routing-profile-out", type=Path,
                        help="where closed-loop promotion writes the learned active profile")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--minimum-effect", type=float, default=0.10)
    parser.add_argument("--post-trials", type=int, default=1)
    args = parser.parse_args()
    if args.trials < 1 or args.max_turns < 1:
        raise SystemExit("trials and max-turns must be positive")
    if bool(args.treatment_profile) == bool(args.experiment_plan):
        raise SystemExit("provide exactly one of --experiment-plan or --treatment-profile")
    command = shlex.split(args.agent_command)
    if not command:
        raise SystemExit("agent command is empty")
    if args.treatment_profile and not args.treatment_profile.exists():
        raise SystemExit(f"treatment profile does not exist: {args.treatment_profile}")
    if args.experiment_plan and not args.experiment_plan.exists():
        raise SystemExit(f"experiment plan does not exist: {args.experiment_plan}")

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
        promoted_path: Path | None = None
        if args.experiment_plan:
            plan = json.loads(args.experiment_plan.read_text(encoding="utf-8"))
            counts = {"control": 0, "treatment": 0}
            index = 0
            while min(counts.values()) < args.trials:
                workspace = Path(state) / "workspaces" / "experiment" / str(index)
                workspace.mkdir(parents=True, exist_ok=True)
                metrics = run_trial(
                    api_module, resolve, command,
                    task_id=f"real-agent-experiment-{index}", profile_path=None,
                    max_turns=args.max_turns, timeout=args.timeout,
                    workspace=workspace, experiment_plan=plan)
                cohort = metrics.get("cohort")
                if cohort in results and counts[cohort] < args.trials:
                    results[cohort].append(metrics)
                    counts[cohort] += 1
                index += 1
            from lib.feedback_loop import load_events  # noqa: E402
            from lib.routing_profiles import derive_profiles, promote_profile  # noqa: E402
            telemetry_root = Path(state) / "cognitiveframeworks" / "telemetry"
            events = []
            for path in sorted(telemetry_root.glob("*.ndjson")):
                events.extend(load_events(path))
            aggregate_module = load("routing_aggregate", DP_ROOT / "scripts" / "aggregate-profiles.py")
            contexts = aggregate_module.load_task_context(telemetry_root)
            learned = derive_profiles(events, contexts, minimum_samples=args.trials,
                                      minimum_effect=args.minimum_effect)
            if len(learned) != 1:
                raise RuntimeError(f"closed-loop routing expected one learned profile, got {len(learned)}")
            candidate = learned[0]
            receipt = candidate["evidence"]["experiment_receipt"]
            if receipt.get("decision") != "pass":
                raise RuntimeError(f"routing experiment did not pass: {receipt}")
            promoted = promote_profile(candidate, receipt)
            promoted_path = args.routing_profile_out or (Path(state) / "active-routing-profile.json")
            promoted_path.write_text(json.dumps(promoted, indent=2) + "\n", encoding="utf-8")
            if args.post_trials:
                results["post"] = []
                for index in range(args.post_trials):
                    workspace = Path(state) / "workspaces" / "post" / str(index)
                    workspace.mkdir(parents=True, exist_ok=True)
                    results["post"].append(run_trial(
                        api_module, resolve, command,
                        task_id=f"real-agent-post-{index}", profile_path=promoted_path,
                        max_turns=args.max_turns, timeout=args.timeout,
                        workspace=workspace))
        else:
            for cohort, profile in (("control", None), ("treatment", args.treatment_profile)):
                for index in range(args.trials):
                    workspace = Path(state) / "workspaces" / cohort / str(index)
                    workspace.mkdir(parents=True, exist_ok=True)
                    results[cohort].append(run_trial(
                        api_module, resolve, command,
                        task_id=f"real-agent-{cohort}-{index}", profile_path=profile,
                        max_turns=args.max_turns, timeout=args.timeout,
                        workspace=workspace))

        def rate(items: list[dict[str, Any]], field: str) -> float:
            return sum(bool(item.get(field)) for item in items) / len(items) if items else 0.0

        control_rate = rate(results["control"], "oracle_success")
        treatment_rate = rate(results["treatment"], "oracle_success")
        effect = treatment_rate - control_rate
        criterion_pass = (
            all(item["finished"] for item in results["treatment"])
            and all(item["finished"] for item in results.get("post", []))
            and treatment_rate >= control_rate + args.minimum_effect
        )
        print(json.dumps({"experiment": "real-agent-control-treatment",
                          "cohort_labels_hidden_from_agent": True,
                          "persistent_agent_process_per_trajectory": True,
                          "objective": {"control_oracle_success_rate": control_rate,
                                        "treatment_oracle_success_rate": treatment_rate,
                                        "effect": effect,
                                        "minimum_effect": args.minimum_effect,
                                        "criterion_pass": criterion_pass},
                          "promoted_profile": str(promoted_path) if promoted_path else None,
                          "results": results}, indent=2))
        return 0 if criterion_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
