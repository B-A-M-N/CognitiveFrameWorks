from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from threading import Event, Thread
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_contract.actions import ActionDescriptor
from runtime_contract.behavioral_fusion import fuse_action_relation
from runtime_contract.protocol_artifact import (
    PROTOCOL_HASH, PROTOCOL_VERSION, QUESTION_REGISTRY_HASH,
    STATE_PROJECTION_SCHEMA_HASH, PROTOCOL_SCHEMA,
)
from runtime_contract.system_one import (
    ActivityFact, QUESTION_REGISTRY, QuestionAnswer, QuestionProbability, SystemOneCapability,
    SystemOneIdentity, SystemOneJudgment,
)
from runtime_contract.task import (AcceptanceCriterion, SystemOnePolicy,
                                  TaskGoalContract, TaskRequest)

TEST_MODEL_CONTENT_HASH = hashlib.sha256(b"test-model-content").hexdigest()
TEST_PROVIDER_PROTOCOL_HASH = hashlib.sha256(b"test-provider-protocol").hexdigest()
TEST_CALIBRATION_HASH = hashlib.sha256(b"test-calibration-bundle").hexdigest()


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


def make_test_policy(mode="shadow", *, calibration_hash=""):
    return SystemOnePolicy(
        mode=mode, backend_id="test-backend", model_id="test-model",
        model_revision="test-revision", model_content_hash=TEST_MODEL_CONTENT_HASH,
        provider_protocol_hash=TEST_PROVIDER_PROTOCOL_HASH,
        calibration_bundle_hash=calibration_hash)


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def test_system_one_latency_policy_allows_bounded_cpu_model_deadline():
    assert SystemOnePolicy(max_latency_ms=30000).max_latency_ms == 30000
    with pytest.raises(ValueError, match="max_latency_ms"):
        SystemOnePolicy(max_latency_ms=30001)


def test_protocol_accepts_relation_certainty_and_unknown_host_telemetry():
    import jsonschema

    activity = ActivityFact(sequence=1, activity_type="action",
                            relation_certainty="unknown").to_dict()
    situation = {
        "current_state": None,
        "blockers": [],
        "actions_since_goal_progress": 0,
        "actions_since_artifact_change": None,
        "verification_actions_since_artifact_change": None,
        "recovery_epochs": 0,
        "repeated_branch_count": 0,
    }

    activity_schema = dict(PROTOCOL_SCHEMA["$defs"]["ActivityFact"])
    activity_schema["$defs"] = PROTOCOL_SCHEMA["$defs"]
    jsonschema.validate(activity, activity_schema)
    situation_schema = dict(
        PROTOCOL_SCHEMA["$defs"]["SemanticState"]["properties"]["situation"])
    situation_schema["$defs"] = PROTOCOL_SCHEMA["$defs"]
    jsonschema.validate(
        situation, situation_schema)


class FixedJudge:
    backend_id = "test-backend"
    model_id = "test-model"
    model_revision = "test-revision"
    model_content_hash = TEST_MODEL_CONTENT_HASH
    provider_protocol_hash = TEST_PROVIDER_PROTOCOL_HASH
    cache_size = 128
    max_latency_ms = 100

    def __init__(self, *, action_relation="unrelated", progress_relation="goal_progress",
                 retry_relation="equivalent_retry", goal_drift="no", wrong_identity=False,
                 calibrated=False, route_preference=None):
        self.frames = []
        self.action_relation = action_relation
        self.progress_relation = progress_relation
        self.retry_relation = retry_relation
        self.goal_drift = goal_drift
        self.route_preference = route_preference
        self.wrong_identity = wrong_identity
        self.calibrated = calibrated
        self.resets = []
        self.resyncs = []

    def capability(self):
        return SystemOneCapability(
            backend_id=self.backend_id, model_id=self.model_id,
            model_revision=self.model_revision,
            model_content_hash=self.model_content_hash,
            provider_protocol_hash=self.provider_protocol_hash,
            protocol_version=PROTOCOL_VERSION, protocol_hash=PROTOCOL_HASH,
            question_registry_hash=QUESTION_REGISTRY_HASH,
            state_projection_schema_hash=STATE_PROJECTION_SCHEMA_HASH,
            supported_question_families=tuple(QUESTION_REGISTRY),
            max_state_bytes=32768, stateful_sessions=True)

    def reset_identity(self, identity):
        self.resets.append(identity)

    def resync(self, frame):
        self.resyncs.append(frame)

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
        selected_by_question = {
            "action_relation": self.action_relation,
            "progress_relation": self.progress_relation,
            "retry_relation": self.retry_relation,
            "goal_drift": self.goal_drift,
            "route_preference": self.route_preference,
        }
        answers = []
        for spec in frame.questions:
            labels = [label for label, _ in spec.criteria]
            selected = selected_by_question.get(spec.question_id, labels[0])
            if selected not in labels:
                selected = labels[0]
            remaining = max(0, len(labels) - 1)
            probabilities = tuple(QuestionProbability(
                label=label,
                probability=(0.99 if label == selected else 0.01 / remaining))
                for label in labels)
            calibrated_probs = probabilities if self.calibrated else None
            answers.append(QuestionAnswer(
                question_id=spec.question_id, selected=selected,
                raw_probabilities=probabilities,
                calibrated_probabilities=calibrated_probs,
                calibration_hash=(frame.calibration_bundle_hash if self.calibrated else None)))
        judgment = SystemOneJudgment(
            identity=identity, request_id=frame.request_id, sequence=frame.sequence,
            state_digest=frame.state_digest,
            question_bundle_digest=frame.question_bundle_digest,
            request_digest=frame.request_digest, policy_hash=frame.policy_hash,
            backend_id=frame.backend_id, model_id=frame.model_id,
            model_revision=frame.model_revision,
            model_content_hash=frame.model_content_hash,
            provider_protocol_hash=frame.provider_protocol_hash,
            calibration_bundle_hash=frame.calibration_bundle_hash,
            answers=tuple(answers), protocol_version=frame.protocol_version,
            protocol_hash=frame.protocol_hash)
        from types import SimpleNamespace
        return SimpleNamespace(judgment=judgment, status="ok", calibrated=self.calibrated)


def request(task_id, *, system_one_policy=None, action_registry=None, support_limit=5):
    goal = TaskGoalContract(
        objective="Change the requested component and verify the stated outcome.",
        deliverable_kind="code_behavior", work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "The requested behavior is present and verified.", "behavior"),))
    return TaskRequest(
        task_id=task_id, application_id="system-one-tests", subject_ref="component:target",
        shape="implement", goal_contract=goal,
        system_one_policy=system_one_policy or make_test_policy(),
        action_registry=action_registry or {},
    )


def test_task_policy_pins_protocol_backend_model_and_calibration_identity():
    resolve, _runtime = runtime_modules()
    policy = make_test_policy()
    bundle = resolve.resolve(request("system-one-policy", system_one_policy=policy))
    assert _plain(bundle.pinned["system_one_policy"]) == policy.to_dict()
    assert _plain(bundle.policy.canonical()["system_one_policy"]) == policy.to_dict()
    assert bundle.pinned["policy_hash"] == resolve.compute_policy_hash(bundle.policy)


def test_shadow_judgment_is_bounded_telemetry_and_cannot_change_action_gate():
    resolve, runtime = runtime_modules()
    bundle = resolve.resolve(request("system-one-shadow"))
    judge = FixedJudge(action_relation="unrelated")
    events = []
    session = runtime.start(bundle, behavioral_judge=judge,
                            telemetry_sink=lambda event: events.append(dict(event)),
                            session_nonce="system-one-shadow")
    startup_event_count = len(events)
    action = {"tool_binding_id": "read", "arguments": {
        "path": "src/main.py", "credential": "raw-secret-marker"},
        "subject_ref": "component:target"}
    preflight = session.preflight_action(action)
    assert preflight.decision == runtime.GateDecision.ALLOW
    assert len(preflight.action_digest) == 64
    assert judge.frames == []
    assert len(events) == startup_event_count
    blocked = session.preflight_action(
        {"tool_binding_id": "shell", "arguments": {},
         "subject_ref": "component:target"})
    assert blocked.decision == runtime.GateDecision.BLOCK
    assert judge.frames == []
    assert len(events) == startup_event_count
    prepared = session.prepare_action(action)
    assert prepared.decision == runtime.GateDecision.ALLOW
    assert len(judge.frames) == 1
    projected = json.dumps(judge.frames[0].canonical_payload(), sort_keys=True)
    assert "raw-secret-marker" not in projected
    assert "src/main.py" not in projected
    decision = next(event for event in events if event["event_type"] == "system_one_decision")
    assert decision["payload"]["system_one_mode"] == "shadow"
    assert decision["payload"]["system_one_action_relation"] == "unrelated"
    assert decision["payload"]["system_one_influenced"] is False
    assert decision["payload"]["system_one_fused_action_relation"] == "diagnostic_support"
    assert decision["payload"]["action_request_id"] == prepared.action_request_id
    assert "raw-secret-marker" not in json.dumps(events)


def test_advisory_directive_requires_pinned_calibration_and_hysteresis():
    resolve, runtime = runtime_modules()
    policy = make_test_policy("advisory", calibration_hash=TEST_CALIBRATION_HASH)
    events = []
    judge = FixedJudge(action_relation="unrelated", goal_drift="yes", calibrated=True)
    session = runtime.start(
        resolve.resolve(request("system-one-advisory", system_one_policy=policy)),
        behavioral_judge=judge,
        telemetry_sink=lambda event: events.append(dict(event)),
        session_nonce="system-one-advisory")
    for _ in range(2):
        assert session.prepare_action({"tool_binding_id": "read", "arguments": {}}).decision \
            == runtime.GateDecision.ALLOW
    context = runtime.context_for(session)
    assert "===== CFW METACOGNITIVE GUIDANCE =====" in context
    assert "Current focus: DELIVER" in context
    assert "A semantic signal suggests possible drift" in context
    decision = next(event for event in events if event["event_type"] == "system_one_decision")
    assert decision["payload"]["system_one_influenced"] is True
    assert decision["payload"]["system_one_answers"]["goal_drift"]["selected"] == "yes"


def test_visual_drift_frame_and_rendered_observation_fuse_to_criterion_progress(tmp_path):
    resolve, runtime = runtime_modules()
    policy = make_test_policy("advisory", calibration_hash=TEST_CALIBRATION_HASH)
    goal = TaskGoalContract(
        objective="Change the visual component to match the supplied reference.",
        deliverable_kind="visual_ui", work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "Visual composition matches the reference", "static_visual"),),
        verification_policy={"support_actions_before_goal_reanchor": 2})
    actions = {
        "visual_edit": ActionDescriptor(
            "visual_edit", mutability="write", action_class="artifact_mutation",
            target_kind="visual_artifact", goal_intent="update the referenced composition",
            affected_criterion_ids=("C1",)),
        "visual_render": ActionDescriptor(
            "visual_render", goal_relation="required_verification", action_class="render",
            target_kind="visual_artifact", goal_intent="render the changed artifact",
            affected_criterion_ids=("C1",)),
    }
    bundle = resolve.resolve(TaskRequest(
        task_id="system-one-visual-render", application_id="system-one-tests",
        subject_ref="repo:visual", shape="implement", goal_contract=goal,
        system_one_policy=policy, action_registry=actions))
    judge = FixedJudge(action_relation="unrelated", progress_relation="goal_progress",
                       goal_drift="yes", calibrated=True)
    events = []
    session = runtime.start(bundle, behavioral_judge=judge,
                            telemetry_sink=lambda event: events.append(dict(event)),
                            session_nonce="system-one-visual-render")
    host = session._host_ingress()

    for index in range(2):
        action = {
            "tool_binding_id": "inspect", "arguments": {},
            "subject_ref": "repo:visual"}
        binding = host.bind_action_objective(action, purpose_class="DIAGNOSTIC",
                                             criterion_ids=("C1",))
        inspection = host.prepare_action(action, objective_binding=binding)
        assert inspection.decision == runtime.GateDecision.ALLOW
        host.record_tool_result(
            "inspect", "success", subject_ref="repo:visual",
            invocation_id=f"visual-inspect-{index}",
            action_request_id=inspection.action_request_id)
    assert session.metacognitive_decision.mode == "REPLAN"
    directive = runtime.context_for(session)
    assert "C1" in directive
    assert "Re-anchor the next action to an unfinished user criterion" in directive
    assert judge.frames[0].state.task["deliverable_kind"] == "visual_ui"
    assert judge.frames[0].state.criteria[0].status == "unmet"

    write_action = {
        "tool_binding_id": "visual_edit",
        "arguments": {"target": str(tmp_path / "buddy-renderer.py")},
        "subject_ref": "repo:visual"}
    write_binding = host.bind_action_objective(
        write_action, purpose_class="DIRECT_GOAL", criterion_ids=("C1",))
    write = host.prepare_action(write_action, objective_binding=write_binding)
    assert write.decision == runtime.GateDecision.ALLOW
    host.record_tool_result(
        "visual_edit", "success", subject_ref="repo:visual",
        invocation_id="visual-edit-1", action_request_id=write.action_request_id,
        actual_result={"artifact_changed": True})
    assert session.convergence.state("primary-objective").support_actions_since_goal_progress == 2
    support_event_count = sum(event["event_type"] == "support_action_recorded"
                              for event in events)

    rendered = host.record_observation("rendered_observation", subject_ref="repo:visual")
    render_action = {
        "tool_binding_id": "visual_render", "arguments": {},
        "subject_ref": "repo:visual"}
    render_binding = host.bind_action_objective(
        render_action, purpose_class="REQUIRED_VERIFICATION", criterion_ids=("C1",))
    render = host.prepare_action(render_action, objective_binding=render_binding)
    assert render.decision == runtime.GateDecision.ALLOW
    host.record_tool_result(
        "visual_render", "success", subject_ref="repo:visual",
        invocation_id="visual-render-1", action_request_id=render.action_request_id,
        actual_result={"artifact_changed": False, "evidence_refs": [rendered]})
    host.record_goal_progress(cause_id="primary-objective", evidence_ref=rendered,
                              criterion_id="C1")

    assert session.convergence.state("primary-objective").support_actions_since_goal_progress == 0
    assert sum(event["event_type"] == "support_action_recorded" for event in events) \
        == support_event_count
    assert session.metacognitive_state.criteria[0].status == "partially_observed"
    progress_frame = next(frame for frame in reversed(judge.frames)
                          if frame.question_ids == ("progress_relation",))
    assert progress_frame.state.task["deliverable_kind"] == "visual_ui"
    assert progress_frame.state.criteria[0].criterion_id == "C1"
    assert progress_frame.state.criteria[0].status == "active"
    assert any(fact.artifact_changed is True and "C1" in fact.affected_criterion_ids
               for fact in progress_frame.state.activity)
    assert "rendered_observation" in progress_frame.state.candidate.evidence_kinds
    fused = [event for event in events if event["event_type"] == "progress_relation_fused"]
    assert fused[-1]["payload"]["progress_relation"] == "goal_progress"
    assert fused[-1]["payload"]["model_influenced"] is True
    credited = next(event for event in events
                    if event["event_type"] == "goal_progress_recorded")
    assert credited["evidence_refs"] == [rendered]
    assert credited["payload"]["result_class"] == "goal_progress"


def _semantic_retry_case(task_id, retry_relation):
    resolve, runtime = runtime_modules()
    policy = make_test_policy("advisory", calibration_hash=TEST_CALIBRATION_HASH)
    goal = TaskGoalContract(
        objective="Fix the visual renderer.", deliverable_kind="visual_ui",
        work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "The visual renderer matches the reference", "static_visual"),))
    bundle = resolve.resolve(TaskRequest(
        task_id=task_id, application_id="system-one-retry", shape="implement",
        goal_contract=goal, system_one_policy=policy))
    judge = FixedJudge(retry_relation=retry_relation, calibrated=True)
    session = runtime.start(bundle, behavioral_judge=judge, telemetry_disabled=True,
                            session_nonce=task_id)
    host = session._host_ingress()
    digest = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    evidence = host.record_observation(
        "diagnostic_observation", subject_ref=bundle.policy.subject_ref)
    host.begin_attempt(
        attempt_ref="attempt-1", cause_id="visual-composition",
        premise_digest=digest("integer-coordinate-hypothesis"),
        action_plan={"tool": "modify_renderer", "mechanism": "integer coordinates"},
        semantic_descriptor={
            "cause_class": "blocky visual composition",
            "hypothesis_summary": "Particles look blocky because positions are rounded to integer pixels.",
            "mechanism": "Quantizing each particle coordinate",
            "evidence_target": "Rendered composition",
            "planned_action_class": "modify_renderer",
            "planned_target_kind": "visual_artifact",
            "affected_criterion_ids": ["C1"], "source": "model_proposed"})
    host.record_attempt_failure(attempt_ref="attempt-1", evidence_ref=evidence)
    assert session.before_retry({
        "tool_binding_id": "inspect", "arguments": {"attempt_ref": "attempt-1"}}) \
        == runtime.GateDecision.REQUIRE_REINSPECTION
    host.record_reinspection(cause_id="visual-composition", revision=1)
    prior_budget = session.convergence.state("visual-composition").same_approach_retries
    host.begin_attempt(
        attempt_ref="attempt-2", cause_id="visual-composition",
        premise_digest=digest("frame-step-hypothesis"),
        action_plan={"tool": "modify_renderer", "mechanism": "frame interpolation"},
        semantic_descriptor={
            "cause_class": "visual texture",
            "hypothesis_summary": "The dots look blocky because rendering holds particles between frame updates.",
            "mechanism": "Frame-stepped interpolation",
            "evidence_target": "Rendered composition",
            "planned_action_class": "modify_renderer",
            "planned_target_kind": "visual_artifact",
            "affected_criterion_ids": ["C1"], "source": "model_proposed"})
    decision = session.before_retry({
        "tool_binding_id": "inspect", "arguments": {"attempt_ref": "attempt-2"}})
    return runtime, session, judge, prior_budget, decision


def test_retry_paraphrase_is_semantically_collapsed_without_budget_reset():
    runtime, session, judge, prior_budget, decision = _semantic_retry_case(
        "system-one-retry-paraphrase", "equivalent_retry")
    assert decision == runtime.GateDecision.REQUIRE_CHANGED_HYPOTHESIS
    frame = judge.frames[-1]
    assert "integer pixels" in frame.state.failed_approaches[-1].hypothesis_summary
    assert "frame updates" in frame.state.candidate.proposed_purpose
    assert len(frame.state.failed_approaches[-1].premise_digest) == 64
    assert len(frame.state.failed_approaches[-1].action_plan_digest) == 64
    assert frame.question_ids == ("retry_relation",)
    assert frame.questions[0].criteria
    assert session.system_one_decisions[judge.frames[-1].request_id]["answers"][
        "retry_relation"]["selected"] == "equivalent_retry"
    assert session.convergence.state("visual-composition").same_approach_retries == prior_budget
    assert session.convergence.state("visual-composition").recovery_epochs == 0


def test_materially_new_retry_remains_eligible_but_does_not_reset_hard_limits():
    runtime, session, judge, prior_budget, decision = _semantic_retry_case(
        "system-one-retry-new-hypothesis", "materially_new")
    assert decision == runtime.GateDecision.ALLOW
    assert session.system_one_decisions[judge.frames[-1].request_id]["answers"][
        "retry_relation"]["selected"] == "materially_new"
    state = session.convergence.state("visual-composition")
    assert state.same_approach_retries == prior_budget
    assert state.recovery_epochs == 0


def test_shadow_prediction_cannot_reset_host_convergence_budget():
    resolve, runtime = runtime_modules()
    bundle = resolve.resolve(request("system-one-no-reset"))
    judge = FixedJudge(progress_relation="goal_progress")
    session = runtime.start(bundle, behavioral_judge=judge, telemetry_disabled=True,
                            session_nonce="system-one-no-reset")
    host = session._host_ingress()
    action = {
        "tool_binding_id": "read", "arguments": {},
        "subject_ref": "component:target"}
    binding = host.bind_action_objective(action, purpose_class="DIAGNOSTIC")
    prepared = host.prepare_action(action, objective_binding=binding)
    assert prepared.decision == runtime.GateDecision.ALLOW
    host.record_tool_result(
        "read", "success", subject_ref="component:target", invocation_id="read-invocation-1",
        action_request_id=prepared.action_request_id,
        actual_result={"secret_result": "not projected", "artifact_changed": True})
    state = session.convergence.state("primary-objective")
    assert state.support_actions_since_goal_progress == 1
    progress_decisions = [decision for decision in session.system_one_decisions.values()
                          if decision["judgment"].get("answers")]
    assert progress_decisions
    assert host.system_one_decision_for_action(
        prepared.action_request_id) == progress_decisions[-1]["decision_id"]
    assert session.convergence.state("primary-objective").support_actions_since_goal_progress == 1


def test_identical_concurrent_requests_get_distinct_correlations():
    resolve, runtime = runtime_modules()
    session = runtime.start(resolve.resolve(request("system-one-concurrent")),
                            behavioral_judge=FixedJudge(), telemetry_disabled=True,
                            session_nonce="system-one-concurrent")
    action = {"tool_binding_id": "read", "arguments": {},
              "subject_ref": "component:target"}
    one, two = session.prepare_action(action), session.prepare_action(action)
    assert one.action_request_id != two.action_request_id
    assert one.decision == two.decision == runtime.GateDecision.ALLOW


def test_stale_system_one_response_is_discarded_when_host_state_advances():
    resolve, runtime = runtime_modules()
    first_started, release_first = Event(), Event()

    class DelayedFirstResponse(FixedJudge):
        def judge(self, frame, *, mode):
            if frame.sequence == 1:
                first_started.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("test did not release the stale provider call")
            return super().judge(frame, mode=mode)

    judge = DelayedFirstResponse()
    session = runtime.start(resolve.resolve(request("system-one-stale-response")),
                            behavioral_judge=judge, telemetry_disabled=True,
                            session_nonce="system-one-stale-response")
    action = {"tool_binding_id": "read", "arguments": {}}
    results, errors = [], []

    def first_call():
        try:
            results.append(session.prepare_action(action))
        except Exception as exc:  # surface worker errors in the main test thread
            errors.append(exc)

    thread = Thread(target=first_call, daemon=True)
    thread.start()
    assert first_started.wait(timeout=5)
    # Advance the host snapshot while semantic inference is in flight. The
    # provider response must be discarded against this newer state revision.
    host = session._host_ingress()
    evidence = host.record_observation(
        "independent_frame", subject_ref="component:target")
    host.record_blocker("concurrent-state-advance", "Host state advanced during inference.",
                        evidence_ref=evidence, criterion_ids=("C1",))
    release_first.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    stale = next(item for item in session.system_one_decisions.values()
                 if item["sequence"] == 1)
    assert stale["status"] == "stale"
    assert stale["status_reason"] == "stale_response"
    assert stale["judgment"] == {}


def test_provider_crash_falls_back_once_without_changing_authority():
    resolve, runtime = runtime_modules()

    class CrashedProvider(FixedJudge):
        def judge(self, frame, *, mode):
            self.frames.append(frame)
            raise RuntimeError("provider process exited")

    judge = CrashedProvider()
    session = runtime.start(resolve.resolve(request("system-one-provider-crash")),
                            behavioral_judge=judge, telemetry_disabled=True,
                            session_nonce="system-one-provider-crash")
    action = session.prepare_action({"tool_binding_id": "read", "arguments": {}})
    assert action.decision == runtime.GateDecision.ALLOW
    fallback = next(iter(session.system_one_decisions.values()))
    assert fallback["status"] == "unavailable"
    assert fallback["status_reason"] == "provider_exception"
    assert fallback["judgment"] == {}
    assert len(judge.frames) == 1
    assert session.prepare_action({"tool_binding_id": "shell", "arguments": {}}).decision \
        == runtime.GateDecision.BLOCK
    assert len(judge.frames) == 1


def test_system_one_disabled_task_runs_without_dp_or_csw():
    resolve, runtime = runtime_modules()
    bundle = resolve.resolve(request("system-one-optional-absent"))
    assert bundle.stateworks == ()
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="system-one-optional-absent")
    assert session.system_one_judge is None
    assert session.prepare_action({"tool_binding_id": "read", "arguments": {}}).decision \
        == runtime.GateDecision.ALLOW
    assert session.prepare_action({"tool_binding_id": "shell", "arguments": {}}).decision \
        == runtime.GateDecision.BLOCK
    assert session.system_one_decisions == {}


def test_multi_agent_system_one_frames_are_identity_and_session_isolated():
    resolve, runtime = runtime_modules()
    goal = TaskGoalContract(
        objective="Change the requested component and verify the stated outcome.",
        deliverable_kind="code_behavior", work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion("C1", "Requested behavior works", "behavior"),))
    frames = []
    sessions = []
    for agent_id in ("worker-a", "worker-b"):
        req = TaskRequest(
            task_id="system-one-multi-agent", application_id="system-one-tests",
            agent_id=agent_id, agent_instance_id=f"{agent_id}-instance",
            subject_ref="component:shared", shape="implement", goal_contract=goal,
            system_one_policy=make_test_policy("shadow"))
        judge = FixedJudge()
        session = runtime.start(
            resolve.resolve(req), behavioral_judge=judge, telemetry_disabled=True,
            session_nonce=f"system-one-{agent_id}")
        prepared = session.prepare_action({
            "tool_binding_id": "read", "arguments": {"target": f"/tmp/{agent_id}.txt"},
            "subject_ref": "component:shared"})
        assert prepared.decision == runtime.GateDecision.ALLOW
        sessions.append(session)
        frames.append(judge.frames[-1])
    assert frames[0].identity.agent_id == "worker-a"
    assert frames[1].identity.agent_id == "worker-b"
    assert frames[0].identity.session_id != frames[1].identity.session_id
    assert frames[0].identity.agent_instance_id != frames[1].identity.agent_instance_id
    assert frames[0].state.candidate.target_ref_digest != frames[1].state.candidate.target_ref_digest
    assert all("worker-b" not in str(fact.to_dict()) for fact in frames[0].state.activity)
    assert all("worker-a" not in str(fact.to_dict()) for fact in frames[1].state.activity)


def test_resume_restores_semantic_projector_and_invalidates_behavior_advice():
    resolve, runtime = runtime_modules()
    judge = FixedJudge(goal_drift="yes")
    bundle = resolve.resolve(request("system-one-resume-projector"))
    session = runtime.start(bundle, behavioral_judge=judge, telemetry_disabled=True,
                            session_nonce="system-one-resume-projector")
    host = session._host_ingress()
    prepared = session.prepare_action({"tool_binding_id": "read", "arguments": {}})
    host.record_tool_result("read", "success", invocation_id="before-resume-read",
                            action_request_id=prepared.action_request_id)
    prior_projection = session.metacognitive_state.snapshot()

    resumed = runtime.resume_task(
        bundle, "system-one-resume-projector", behavioral_judge=judge,
        telemetry_disabled=True)
    assert resumed.metacognitive_state.snapshot() == prior_projection
    assert resumed.metacognitive_decision.mode == "DELIVER"
    assert "CURRENT EXECUTION FOCUS" not in runtime.context_for(resumed)
    assert judge.resets
    next_action = resumed.prepare_action({"tool_binding_id": "read", "arguments": {}})
    assert next_action.decision == runtime.GateDecision.ALLOW
    frame = judge.frames[-1]
    assert any(fact.activity_type == "action"
               and fact.result_class == "success" for fact in frame.state.activity)


def test_system_one_outcome_requires_later_independent_evidence():
    resolve, runtime = runtime_modules()
    events = []
    session = runtime.start(
        resolve.resolve(request("system-one-outcome")), behavioral_judge=FixedJudge(),
        telemetry_sink=lambda event: events.append(dict(event)),
        session_nonce="system-one-outcome")
    host = session._host_ingress()
    prepared = session.prepare_action({
        "tool_binding_id": "read", "arguments": {},
        "subject_ref": "component:target"})
    decision_id = host.system_one_decision_for_action(prepared.action_request_id)
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
    assert session.prepare_action({"tool_binding_id": "read", "arguments": {}}).decision \
        == runtime.GateDecision.ALLOW
    assert len(judge.frames) == 1
    decision = next(event for event in events if event["event_type"] == "system_one_decision")
    assert decision["payload"]["system_one_status"] == "unavailable"
    assert decision["payload"]["system_one_status_reason"] == "provider_exception"
    assert session.prepare_action({"tool_binding_id": "shell", "arguments": {}}).decision \
        == runtime.GateDecision.BLOCK
    assert len(judge.frames) == 1


def test_static_host_relations_do_not_depend_on_tool_names_or_model_labels():
    policy = make_test_policy("advisory", calibration_hash=TEST_CALIBRATION_HASH)
    identity = SystemOneIdentity("ns", "app", "app-instance", "agent", "agent-instance",
                                 "session", "task", "attempt")
    labels = list(QUESTION_REGISTRY["action_relation"]["criteria"])
    probs = tuple(QuestionProbability(label, 1 / len(labels)) for label in labels)
    answer = QuestionAnswer("action_relation", "advances_primary_goal", probs,
                            probs, TEST_CALIBRATION_HASH)
    judgment = SystemOneJudgment(
        identity=identity, request_id="request", sequence=1, state_digest="b" * 64,
        question_bundle_digest="d" * 64, request_digest="e" * 64,
        policy_hash="c" * 64, backend_id="test-backend", model_id="test-model",
        model_revision="test-revision", model_content_hash=TEST_MODEL_CONTENT_HASH,
        provider_protocol_hash=TEST_PROVIDER_PROTOCOL_HASH,
        calibration_bundle_hash=TEST_CALIBRATION_HASH, answers=(answer,))
    cases = (
        (ActionDescriptor("render", goal_relation="required_verification"),
         "required_verification"),
        (ActionDescriptor("test", goal_relation="diagnostic_support"),
         "diagnostic_support"),
        (ActionDescriptor("write_unrelated", mutability="write", goal_relation="unrelated"),
         "unrelated"),
    )
    for descriptor, expected in cases:
        fused = fuse_action_relation(descriptor, judgment=judgment, policy=policy, calibrated=True)
        assert fused.relation == expected
        assert fused.source == "host_descriptor"
        assert fused.model_influenced is False


def test_resume_rejects_changed_system_one_policy_identity():
    resolve, runtime = runtime_modules()
    first = resolve.resolve(request("system-one-resume"))
    runtime.start(first, telemetry_disabled=True, session_nonce="system-one-resume")
    different = SystemOnePolicy(**{
        **make_test_policy().to_dict(), "model_id": "test-model-v2"})
    second = resolve.resolve(request("system-one-resume", system_one_policy=different))
    with pytest.raises(PermissionError, match="journal is not bound"):
        runtime.resume_task(second, "system-one-resume", telemetry_disabled=True)


def test_provider_identity_must_match_policy_before_start():
    resolve, runtime = runtime_modules()
    bundle = resolve.resolve(request("system-one-provider-mismatch"))
    judge = FixedJudge()
    judge.model_id = "changed-model"
    with pytest.raises(ValueError, match="capability model_id differs"):
        runtime.start(bundle, behavioral_judge=judge, telemetry_disabled=True)


def _route_epoch_bundles(resolve, task_id, policy):
    cow = Path(resolve.DEFAULT_STATEWORK_ROOT)
    request = resolve.TaskRequest(
        task_id=task_id, application_id="system-one-route-epoch",
        subject_ref="infra:route-epoch", shape="implement",
        domain_tags=("infrastructure",),
        observation_context={"state": "PLANNED"}, system_one_policy=policy,
        goal_contract=TaskGoalContract(
            objective="Safely change and validate the infrastructure state.",
            deliverable_kind="code_behavior", work_mode="delivery",
            acceptance_criteria=(AcceptanceCriterion(
                "C1", "The selected infrastructure change is validated.", "behavior"),)))
    routes = resolve.enumerate_eligible_routes(
        next(item for item in resolve.load_stateworks(cow) if item["id"] == "infrae"),
        cow, current_state="PLANNED", task_shape="implement",
        domain_tags=("infrastructure",))
    children = {
        f"flow:infrae:{item['route']}": resolve.resolve(
            request, cow_root=cow, requested_flows={"infrae": item["route"]})
        for item in routes if item["route_type"] == "flow"
    }
    parent = children["flow:infrae:change"]
    return request, parent, children


def test_policy_epoch_ranks_host_resolved_routes_and_starts_immutable_child(tmp_path):
    resolve, runtime = runtime_modules()
    policy = make_test_policy("advisory", calibration_hash=TEST_CALIBRATION_HASH)
    request_contract, parent, children = _route_epoch_bundles(
        resolve, "system-one-route-epoch", policy)
    target = "flow:infrae:incident"
    judge = FixedJudge(goal_drift="yes", calibrated=True,
                       route_preference=target)
    authority = runtime.create_state_authority(
        Path(resolve.DEFAULT_STATEWORK_ROOT), state_path=tmp_path / "subjects.json",
        evidence_path=tmp_path / "evidence.json")
    session = runtime.start(parent, behavioral_judge=judge, state_authority=authority,
                            telemetry_disabled=True,
                            session_nonce="system-one-route-epoch")
    host = session._host_ingress()
    for index in range(5):
        action = {
            "tool_binding_id": "read", "arguments": {},
            "subject_ref": parent.policy.subject_ref}
        binding = host.bind_action_objective(action, purpose_class="DIAGNOSTIC")
        prepared = host.prepare_action(action, objective_binding=binding)
        assert prepared.decision == runtime.GateDecision.ALLOW
        host.record_tool_result(
            "read", "success", subject_ref=parent.policy.subject_ref,
            invocation_id=f"route-epoch-read-{index}",
            action_request_id=prepared.action_request_id)
    assert session.metacognitive_decision.mode == "REPLAN"

    recommendation = host.rank_legal_routes(
        children, state_revision=7, target_statework_id="infrae")
    assert recommendation is not None
    assert recommendation.route == target
    assert set(recommendation.legal_children) == set(children)
    with pytest.raises(PermissionError, match="revision changed"):
        host.create_child_policy_epoch(
            recommendation, selected_route=target, current_state_revision=8)

    epoch = host.create_child_policy_epoch(
        recommendation, selected_route=target, current_state_revision=7)
    assert epoch.record.parent_policy_hash == parent.pinned["policy_hash"]
    assert epoch.record.child_policy_hash == epoch.bundle.pinned["policy_hash"]
    assert epoch.record.system_one_decision_ref == recommendation.decision_id
    assert epoch.bundle is children[target]
    assert parent.policy.stateworks[0].selected_flow != epoch.bundle.policy.stateworks[0].selected_flow

    child_session = runtime.start(
        epoch.bundle, behavioral_judge=judge, state_authority=authority,
        telemetry_disabled=True,
        session_nonce="system-one-route-epoch-child",
        policy_epoch_record=epoch.record)
    assert child_session.bundle.pinned["policy_hash"] == epoch.record.child_policy_hash
    assert child_session.policy_epoch_record == epoch.record
    assert session.bundle is parent
    assert session.bundle.pinned["policy_hash"] == epoch.record.parent_policy_hash
    resumed_child = runtime.resume_task(
        epoch.bundle, "system-one-route-epoch-child",
        behavioral_judge=judge, state_authority=authority, telemetry_disabled=True)
    assert resumed_child.policy_epoch_record == epoch.record


def test_policy_epoch_rejects_an_unresolved_or_forged_route_bundle(tmp_path):
    resolve, runtime = runtime_modules()
    policy = make_test_policy("advisory", calibration_hash=TEST_CALIBRATION_HASH)
    _request_contract, parent, children = _route_epoch_bundles(
        resolve, "system-one-route-epoch-invalid", policy)
    authority = runtime.create_state_authority(
        Path(resolve.DEFAULT_STATEWORK_ROOT), state_path=tmp_path / "subjects.json",
        evidence_path=tmp_path / "evidence.json")
    session = runtime.start(parent, state_authority=authority,
                            behavioral_judge=FixedJudge(goal_drift="yes", calibrated=True),
                            telemetry_disabled=True,
                            session_nonce="system-one-route-epoch-invalid")
    host = session._host_ingress()
    for index in range(5):
        action = {
            "tool_binding_id": "read", "arguments": {},
            "subject_ref": parent.policy.subject_ref}
        binding = host.bind_action_objective(action, purpose_class="DIAGNOSTIC")
        prepared = host.prepare_action(action, objective_binding=binding)
        host.record_tool_result(
            "read", "success", subject_ref=parent.policy.subject_ref,
            invocation_id=f"route-epoch-invalid-{index}",
            action_request_id=prepared.action_request_id)
    forged = dict(children)
    forged["flow:infrae:illegal"] = children["flow:infrae:incident"]
    with pytest.raises(ValueError, match="selected legal flow"):
        host.rank_legal_routes(forged, state_revision=0, target_statework_id="infrae")
    assert session.bundle is parent
