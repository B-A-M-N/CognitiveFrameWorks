#!/usr/bin/env python3
"""Prove session isolation and DP -> CFW routing-profile consumption.

This is a contract acceptance harness, not a claim of model behavior
improvement.  The real-agent harness is separate and requires an explicit
host agent command.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DP_ROOT = ROOT.parent / "DigitalPsychology"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(DP_ROOT))
from lib.feedback_loop import load_events  # noqa: E402
from lib.trajectories import build_trajectories  # noqa: E402
from lib.routing_profiles import derive_profiles, promote_profile  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cfw-session-routing-") as raw:
        raw_root = Path(raw)
        os.environ["XDG_RUNTIME_DIR"] = str(raw_root / "runtime")
        os.environ["DIGITALPSYCHOLOGY_STATE_ROOT"] = str(raw_root / "dp-state")
        resolve = load("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
        runtime = load("runtime", ROOT / "scripts" / "runtime.py")
        api_module = load("cognitive_runtime", ROOT / "scripts" / "cognitive_runtime.py")
        api = api_module.CognitiveRuntime()

        # Same logical task and agent, concurrent runtime sessions: they must
        # produce distinct analytics identities and distinct telemetry files.
        same_request = resolve.TaskRequest(
            task_id="same-logical-task", subject_ref="subject:same",
            shape="quick", agent_id="agent-a", agent_instance_id="agent-a-instance")
        first = api.begin_task(same_request)
        second = api.begin_task(same_request)
        assert first.session_id != second.session_id
        host_first = api.host_handle(first)
        host_second = api.host_handle(second)
        host_first.route_outcome(route="flow", route_type="stage", outcome="bad")
        host_second.route_outcome(route="flow", route_type="stage", outcome="good")

        # Ten control and ten treatment sessions provide enough evidence for
        # the profile builder, while every event remains session-local.
        sessions = [first, second]
        for index in range(20):
            cohort = "control" if index < 10 else "treatment"
            request = resolve.TaskRequest(
                task_id=f"route-task-{index}", subject_ref=f"subject:{index}",
                shape="performance", model="model-a", harness="harvardcodex",
                agent_id="agent-a", agent_instance_id="agent-a-instance")
            session = api.begin_task(request)
            api.host_handle(session).route_outcome(
                route="flow", route_type="stage",
                outcome="bad" if cohort == "control" else "good")
            sessions.append(session)

        telemetry_root = Path(os.environ["XDG_RUNTIME_DIR"]) / "cognitiveframeworks" / "telemetry"
        events = []
        for path in sorted(telemetry_root.glob("*.ndjson")):
            events.extend(load_events(path))
        episodes, tasks, session_trajectories, agents = build_trajectories(events)
        assert len({event.session_id for event in events}) == len({session.session_id for session in sessions})
        same_task_sessions = [item for item in session_trajectories if item.session_id in {first.session_id, second.session_id}]
        assert len(same_task_sessions) == 2
        assert all(len(item.tasks) == 1 and item.tasks[0].task_id == "same-logical-task"
                   for item in same_task_sessions)

        contexts = {}
        for index in range(20):
            session = sessions[index + 2]
            contexts[f"{session.session_id}:route-task-{index}"] = {
                "task_id": f"route-task-{index}", "session_id": session.session_id,
                "agent_instance_id": "agent-a-instance", "model": "model-a",
                "harness": "harvardcodex", "task_family": "performance",
                "task_shape": "performance", "environment": "runtime",
                "cohort": "control" if index < 10 else "treatment",
            }
        learned = derive_profiles(events, contexts)
        assert len(learned) == 1, learned
        candidate = learned[0]
        assert candidate["status"] == "candidate"
        assert candidate["routing_adjustments"][0]["disposition"] == "suppress"

        candidate_path = raw_root / "candidate-routing-profile.json"
        candidate_path.write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
        try:
            api_module.CognitiveRuntime(
                routing_profile_path=str(candidate_path)).begin_task(resolve.TaskRequest(
                    task_id="candidate-must-not-deploy", subject_ref="subject:candidate",
                    shape="performance", model="model-a", harness="harvardcodex",
                    agent_id="agent-a", agent_instance_id="agent-a-instance"))
            raise AssertionError("candidate routing profile deployed without validation")
        except ValueError:
            pass

        # A host/operator promotion step can make the candidate deployable;
        # CFW still validates the artifact and applies it only to its scope.
        candidate = promote_profile(candidate, "routing-receipt-session-routing")
        profile_path = raw_root / "active-routing-profile.json"
        profile_path.write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
        profiled = resolve.TaskRequest(
            task_id="profiled-task", subject_ref="subject:profiled", shape="performance",
            model="model-a", harness="harvardcodex", agent_id="agent-a",
            agent_instance_id="agent-a-instance")
        profiled_bundle = api_module.CognitiveRuntime(
            routing_profile_path=str(profile_path)).begin_task(profiled).bundle
        assert "flow" not in profiled_bundle.kernel
        assert profiled_bundle.policy.routing_profile_hash == candidate["profile_hash"]
        print("SESSION_TRAJECTORY_ROUTING_ACCEPTANCE=PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
