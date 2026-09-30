from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_contract.actions import ActionDescriptor
from runtime_contract.behavioral_fusion import fuse_action_relation
from runtime_contract.system_one import SystemOneIdentity, SystemOneJudgment
from runtime_contract.task import (AcceptanceCriterion, SystemOnePolicy,
                                  TaskGoalContract, TaskRequest)


def runtime_modules():
    sys.modules.pop("resolve_runtime", None)
    sys.modules.pop("runtime", None)
    resolve_spec = importlib.util.spec_from_file_location(
        "resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
    resolve = importlib.util.module_from_spec(resolve_spec)
    sys.modules["resolve_runtime"] = resolve
    resolve_spec.loader.exec_module(resolve)
    runtime_spec = importlib.util.spec_from_file_location(
        "runtime", ROOT / "scripts" / "runtime.py")
    runtime = importlib.util.module_from_spec(runtime_spec)
    sys.modules["runtime"] = runtime
    runtime_spec.loader.exec_module(runtime)
    return resolve, runtime


class FixedJudge:
    backend_id = "test-backend"
    model_id = "test-model"

    def __init__(self, *, action_relation="unrelated", progress_relation="goal_progress",
                 retry_relation="equivalent_retry", goal_drift="no", wrong_identity=False,
                 calibration_hash="", calibrated=False):
        self.frames = []
        self.action_relation = action_relation
        self.progress_relation = progress_relation
        self.retry_relation = retry_relation
        self.goal_drift = goal_drift
        self.wrong_identity = wrong_identity
        self.calibration_hash = calibration_hash
        self.calibrated = calibrated

    def judge(self, frame, *, mode):
        self.frames.append(frame)
        identity = frame.identity
        if self.wrong_identity:
            identity = SystemOneIdentity(
                namespace_id=identity.namespace_id, application_id=identity.application_id,
                application_instance_id=identity.application_instance_id, agent_id="other-agent",
                agent_instance_id=identity.agent_instance_id, session_id=identity.session_id,
                task_id=identity.task_id, attempt_id=identity.attempt_id,
                interaction_id=identity.interaction_id)
        answer = SystemOneJudgment(
            identity=identity, request_id=frame.request_id, sequence=frame.sequence,
            state_digest=frame.state_digest, policy_hash=frame.policy_hash,
            backend_id=frame.backend_id, model_id=frame.model_id,
            calibration_hash=frame.calibration_hash, confidence=0.99,
            action_relation=(self.action_relation if "action_relation" in frame.questions else None),
            progress_relation=(self.progress_relation if "progress_relation" in frame.questions else None),
            retry_relation=(self.retry_relation if "retry_relation" in frame.questions else None),
            goal_drift=(self.goal_drift if "goal_drift" in frame.questions else None),
            protocol_version=frame.protocol_version, protocol_hash=frame.protocol_hash)
        if self.calibrated:
            from types import SimpleNamespace
            return SimpleNamespace(judgment=answer, status="ok", calibrated=True)
        return answer


def request(task_id, *, system_one_policy=None, action_registry=None, support_limit=5):
    goal = TaskGoalContract(
        objective="Change the requested component and verify the stated outcome.",
        deliverable_kind="code_behavior", work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "The requested behavior is present and verified.", "behavior"),))
    return TaskRequest(
        task_id=task_id, application_id="system-one-tests", subject_ref="component:target",
        shape="implement", goal_contract=goal,
        system_one_policy=system_one_policy or SystemOnePolicy(
            mode="shadow", backend_id="test-backend", model_id="test-model"),
        action_registry=action_registry or {},
        )


def test_task_policy_pins_protocol_backend_model_and_calibration_identity():
    resolve, _runtime = runtime_modules()
    policy = SystemOnePolicy(mode="shadow", backend_id="test-backend", model_id="test-model")
    bundle = resolve.resolve(request("system-one-policy", system_one_policy=policy))
    assert bundle.pinned["system_one_policy"] == policy.to_dict()
    assert bundle.policy.canonical()["system_one_policy"] == policy.to_dict()
    assert bundle.pinned["policy_hash"] == resolve.compute_policy_hash(bundle.policy)


def test_shadow_judgment_is_bounded_telemetry_and_cannot_change_action_gate():
    resolve, runtime = runtime_modules()
    bundle = resolve.resolve(request("system-one-shadow"))
    judge = FixedJudge(action_relation="unrelated")
    events = []
    session = runtime.start(bundle, behavioral_judge=judge,
                            telemetry_sink=lambda event: events.append(dict(event)),
                            session_nonce="system-one-shadow")
    action = {"tool_binding_id": "read", "arguments": {
        "path": "src/main.py", "credential": "raw-secret-marker"},
        "subject_ref": "component:target"}
    assert session.before_action(action) == runtime.GateDecision.ALLOW
    assert len(judge.frames) == 1
    projected = json.dumps(judge.frames[0].canonical_payload(), sort_keys=True)
    assert "raw-secret-marker" not in projected
    decision = next(event for event in events if event["event_type"] == "system_one_decision")
    assert decision["payload"]["system_one_mode"] == "shadow"
    assert decision["payload"]["system_one_action_relation"] == "unrelated"
    assert decision["payload"]["system_one_influenced"] is False
    assert decision["payload"]["system_one_fused_action_relation"] == "diagnostic_support"
    assert "raw-secret-marker" not in json.dumps(events)


def test_advisory_guidance_requires_pinned_calibration_and_stays_typed():
    resolve, runtime = runtime_modules()
    policy = SystemOnePolicy(
        mode="advisory", backend_id="test-backend", model_id="test-model",
        calibration_hash="a" * 64)
    events = []
    judge = FixedJudge(action_relation="unrelated", goal_drift="yes",
                       calibration_hash="a" * 64, calibrated=True)
    session = runtime.start(
        resolve.resolve(request("system-one-advisory", system_one_policy=policy)),
        behavioral_judge=judge,
        telemetry_sink=lambda event: events.append(dict(event)),
        session_nonce="system-one-advisory")
    assert session.before_action({"tool_binding_id": "read", "arguments": {}}) == runtime.GateDecision.ALLOW
    context = runtime.context_for(session)
    assert "CALIBRATED SYSTEM-1 ADVISORY" in context
    assert "Possible goal drift" in context
    decision = next(event for event in events if event["event_type"] == "system_one_decision")
    assert decision["payload"]["system_one_influenced"] is True


def test_shadow_prediction_cannot_reset_host_convergence_budget():
    resolve, runtime = runtime_modules()
    bundle = resolve.resolve(request("system-one-no-reset"))
    judge = FixedJudge(progress_relation="goal_progress")
    session = runtime.start(bundle, behavioral_judge=judge, telemetry_disabled=True,
                            session_nonce="system-one-no-reset")
    host = session._host_ingress()
    evidence_ref = host.record_tool_result(
        "write", "success", subject_ref="component:target", invocation_id="write-1",
        action_digest=runtime.action_digest("write", {}, "component:target"),
        actual_result={"secret_result": "not projected"})
    state = session.convergence.state("primary-objective")
    assert state.support_actions_since_goal_progress == 1
    progress_decisions = [decision for decision in session.system_one_decisions.values()
                          if decision["judgment"].get("progress_relation") == "goal_progress"]
    assert progress_decisions
    assert host.system_one_decision_for_action(
        progress_decisions[-1]["action_digest"]) == progress_decisions[-1]["decision_id"]
    host.record_goal_progress(cause_id="primary-objective", evidence_ref=evidence_ref,
                              criterion_id="C1")
    assert session.convergence.state("primary-objective").support_actions_since_goal_progress == 0


def test_system_one_outcome_requires_later_independent_evidence():
    resolve, runtime = runtime_modules()
    events = []
    session = runtime.start(
        resolve.resolve(request("system-one-outcome")), behavioral_judge=FixedJudge(),
        telemetry_sink=lambda event: events.append(dict(event)),
        session_nonce="system-one-outcome")
    host = session._host_ingress()
    action = {"tool_binding_id": "read", "arguments": {},
              "subject_ref": "component:target"}
    session.before_action(action)
    action_digest = runtime.action_digest("read", {}, "component:target")
    decision_id = host.system_one_decision_for_action(action_digest)
    assert decision_id
    evidence = host.record_observation("independent_result", "component:target")
    event_id = host.record_system_one_outcome(
        decision_id=decision_id, label="action_relation", value="diagnostic_support",
        evidence_ref=evidence, authority="host_evidence")
    outcome = next(event for event in events if event.get("event_id") == event_id)
    assert outcome["payload"]["system_one_independent_outcome"] is True
    assert outcome["payload"]["system_one_outcome_for"] == decision_id
    assert outcome["evidence_refs"] == [evidence]
    with pytest.raises(ValueError, match="already recorded"):
        host.record_system_one_outcome(
            decision_id=decision_id, label="action_relation", value="diagnostic_support",
            evidence_ref=evidence, authority="host_evidence")


def test_stale_identity_falls_back_and_ward_block_precedes_semantic_query():
    resolve, runtime = runtime_modules()
    judge = FixedJudge(wrong_identity=True)
    events = []
    session = runtime.start(
        resolve.resolve(request("system-one-correlation")), behavioral_judge=judge,
        telemetry_sink=lambda event: events.append(dict(event)),
        session_nonce="system-one-correlation")
    assert session.before_action({"tool_binding_id": "read", "arguments": {}}) == runtime.GateDecision.ALLOW
    assert len(judge.frames) == 1
    assert next(event for event in events if event["event_type"] == "system_one_decision")[
        "payload"]["system_one_status"] == "fallback"
    assert session.before_action({"tool_binding_id": "shell", "arguments": {}}) == runtime.GateDecision.BLOCK
    assert len(judge.frames) == 1


def test_static_host_relations_do_not_depend_on_tool_names_or_model_labels():
    policy = SystemOnePolicy(mode="advisory", backend_id="test-backend",
                             model_id="test-model", calibration_hash="a" * 64)
    identity = SystemOneIdentity("ns", "app", "app-instance", "agent", "agent-instance",
                                 "session", "task", "attempt")
    answer = SystemOneJudgment(
        identity=identity, request_id="request", sequence=1, state_digest="b" * 64,
        policy_hash="c" * 64, backend_id="test-backend", model_id="test-model",
        calibration_hash="a" * 64, confidence=0.99,
        action_relation="advances_primary_goal")
    cases = (
        (ActionDescriptor("render", goal_relation="required_verification"),
         "required_verification"),
        (ActionDescriptor("test", goal_relation="diagnostic_support"),
         "diagnostic_support"),
        (ActionDescriptor("write_unrelated", mutability="write", goal_relation="unrelated"),
         "unrelated"),
    )
    for descriptor, expected in cases:
        fused = fuse_action_relation(descriptor, judgment=answer, policy=policy, calibrated=True)
        assert fused.relation == expected
        assert fused.source == "host_descriptor"
        assert fused.model_influenced is False


def test_resume_rejects_changed_system_one_policy_identity():
    resolve, runtime = runtime_modules()
    first = resolve.resolve(request("system-one-resume"))
    runtime.start(first, telemetry_disabled=True, session_nonce="system-one-resume")
    different = SystemOnePolicy(mode="shadow", backend_id="test-backend",
                                model_id="test-model-v2")
    second = resolve.resolve(request("system-one-resume", system_one_policy=different))
    with pytest.raises(PermissionError, match="journal is not bound"):
        runtime.resume_task(second, "system-one-resume", telemetry_disabled=True)


def test_provider_identity_must_match_policy_before_start():
    resolve, runtime = runtime_modules()
    bundle = resolve.resolve(request("system-one-provider-mismatch"))
    judge = FixedJudge()
    judge.model_id = "changed-model"
    with pytest.raises(ValueError, match="model_id differs from pinned policy"):
        runtime.start(bundle, behavioral_judge=judge, telemetry_disabled=True)
