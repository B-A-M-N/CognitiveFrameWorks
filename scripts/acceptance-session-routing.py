#!/usr/bin/env python3
"""Closed-loop routing experiment acceptance.

The host predeclares an intervention and assigns independent sessions. DP
derives a candidate and receipt only from host route decisions and evaluator
outcomes.
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
from lib.routing_profiles import (build_experiment_plan, build_profile,
                                  derive_profiles, promote_profile)  # noqa: E402
from runtime_contract.routing import comparison_context_hash  # noqa: E402


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
        load("runtime", ROOT / "scripts" / "runtime.py")
        api_module = load("cognitive_runtime", ROOT / "scripts" / "cognitive_runtime.py")
        api = api_module.CognitiveRuntime()

        model = "model-a"
        harness = "harvardcodex"
        task_family = "performance"
        comparison_hash = comparison_context_hash({
            "agent_instance_id": "agent-a-instance", "model": model,
            "harness": harness, "toolset": None, "task_family": task_family,
            "task_shape": task_family, "domain_tags": [], "phase": None,
            "environment": "runtime",
            "statework_versions": "{}", "framework_version": "1.4.0",
            "guard_pack_hash": resolve.load_guard_pack()["semantic_hash"],
        })
        adjustment = {
            "route_type": "stage", "route": "flow", "statework_id": None,
            "disposition": "suppress", "condition": {},
            "evidence_refs": ["predeclared-experiment"], "confidence": 1.0,
            "expires_after_samples": 20,
        }
        candidate = build_profile(
            profile_id="routing-session-flow-suppression",
            subject={"agent_instance_id": "agent-a-instance", "model": model,
                     "harness": harness},
            context={"task_family": task_family, "task_shape": task_family,
                     "domain_tags": [], "phase": None, "environment": "runtime",
                     "toolset": None,
                     "statework_versions": "{}", "framework_version": "1.4.0",
                     "guard_pack_hash": resolve.load_guard_pack()["semantic_hash"]},
            observations=[], routing_adjustments=[adjustment],
            evidence={"event_ids": ["preexperiment"], "trajectory_ids": [],
                      "source_hash": "preexperiment",
                      "experiment_id": "routing-session-closed-loop",
                      "candidate_adjustment": adjustment, "receipt_ref": None},
            criterion={"evaluator_version": "routing-outcome-v2",
                       "minimum_samples": 10, "minimum_effect": 0.10,
                       "confidence_rule": "independent trajectory effect lower confidence bound is non-negative"})
        plan = build_experiment_plan(
            experiment_id="routing-session-closed-loop",
            candidate_profile_hash=candidate["profile_hash"],
            candidate_profile_id=candidate["profile_id"],
            candidate_adjustment=adjustment,
            eligibility={"field": "task_family", "equals": task_family},
            target_evaluator="route-success-v1",
            control_policy_identity="static-control",
            treatment_policy_identity="candidate-treatment",
            holdout_requirements={"regressions": []}, canary_fraction=0.5)

        sessions = []
        counts = {"control": 0, "treatment": 0}
        index = 0
        while min(counts.values()) < 10:
            request = resolve.TaskRequest(
                task_id=f"route-task-{index}", subject_ref=f"subject:{index}",
                shape=task_family, model=model, harness=harness,
                agent_id="agent-a", agent_instance_id="agent-a-instance",
                routing_experiment_plan=plan)
            session = api.begin_task(request)
            experiment = session.bundle.guard_pack["routing_experiment"]
            cohort = experiment["routing_cohort"]
            if counts[cohort] >= 10:
                index += 1
                continue
            host = api.host_handle(session)
            decision_ids = [decision_id for decision_id, decision in session.route_decisions.items()
                            if decision["route"] == "flow" and decision["route_type"] == "stage"]
            assert decision_ids, (cohort, session.route_decisions)
            invocation_id = f"route-invocation-{index}"
            actual = {"status": "pass" if cohort == "treatment" else "fail",
                      "fresh": True, "relevant": True,
                      "fixture": "same-task-oracle"}
            host.after_tool(tool_type="fixture-task", result_class=actual["status"],
                            subject_ref=request.subject_ref, invocation_id=invocation_id,
                            actual_result=actual)
            evidence_id = host.after_validator(
                validator_id="reinspection-v1", invocation_id=invocation_id,
                result=actual, subject_ref=request.subject_ref)
            host.evaluate_route_outcome(
                route_decision_id=decision_ids[0], evaluator_id="route-success-v1",
                evidence_refs=[evidence_id])
            sessions.append(session)
            counts[cohort] += 1
            index += 1

        telemetry_root = Path(os.environ["XDG_RUNTIME_DIR"]) / "cognitiveframeworks" / "telemetry"
        events = []
        for path in sorted(telemetry_root.glob("*.ndjson")):
            events.extend(load_events(path))
        contexts = {}
        for session in sessions:
            experiment = dict(session.bundle.guard_pack["routing_experiment"])
            contexts[f"{session.session_id}:{session.bundle.task_id}:{session.attempt_id}"] = {
                "task_id": session.bundle.task_id, "session_id": session.session_id,
                "attempt_id": session.attempt_id, "agent_instance_id": session.agent_instance_id,
                "model": model, "harness": harness, "task_family": task_family,
                "task_shape": task_family, "domain_tags": [], "phase": None,
                "environment": "runtime", "toolset": None,
                "policy_hash": session.bundle.pinned["policy_hash"],
                "statework_versions": "{}", "framework_version": "1.4.0",
                "guard_pack_hash": session.bundle.pinned["guard_pack_hash"],
                "routing_experiment_plan": plan,
                "routing_cohort": experiment["routing_cohort"],
                "candidate_profile_hash": experiment["candidate_profile_hash"],
                "candidate_adjustment_applied": experiment["candidate_adjustment_applied"],
                "comparison_context_hash": experiment["comparison_context_hash"],
            }

        learned = derive_profiles(events, contexts, minimum_samples=10)
        assert len(learned) == 1, learned
        candidate = learned[0]
        assert candidate["status"] == "candidate"
        receipt = candidate["evidence"]["experiment_receipt"]
        assert receipt["decision"] == "pass"
        promoted = promote_profile(candidate, receipt)
        profile_path = raw_root / "active-routing-profile.json"
        profile_path.write_text(json.dumps(promoted, indent=2) + "\n", encoding="utf-8")
        profiled = resolve.TaskRequest(
            task_id="profiled-task", subject_ref="subject:profiled", shape=task_family,
            model=model, harness=harness, agent_id="agent-a",
            agent_instance_id="agent-a-instance")
        profiled_bundle = api_module.CognitiveRuntime(
            routing_profile_path=str(profile_path)).begin_task(profiled).bundle
        assert "flow" not in profiled_bundle.kernel
        assert profiled_bundle.policy.routing_profile_hash
        print("SESSION_TRAJECTORY_ROUTING_ACCEPTANCE=PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
