#!/usr/bin/env python3
"""Hostile A/B/C runtime qualification with controlled semantic fixtures.

The test backend supplies known answers through DigitalPsychology's real
SystemOneRuntime, protocol validation, and calibration path. Fixture
calibration records exercise advisory gates; they do not qualify a model or
represent outcome-derived production calibration.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
COW = ROOT.parent / "CognitiveStateWork"
DIGITAL_PSYCHOLOGY = ROOT.parent / "DigitalPsychology"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(DIGITAL_PSYCHOLOGY))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


resolve = load_module("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
runtime = load_module("runtime", ROOT / "scripts" / "runtime.py")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


FIXTURE_BACKEND_ID = "ablation-fixture"
FIXTURE_MODEL_ID = "controlled-semantic-answers"
FIXTURE_MODEL_REVISION = "fixture-v1"
FIXTURE_MODEL_CONTENT_HASH = digest("ablation fixture model")
FIXTURE_PROVIDER_PROTOCOL_HASH = digest("ablation fixture protocol")


def fixture_calibration_bundle():
    """Build controlled records to exercise DP's real calibration checks."""
    from lib.protocol_artifact import QUESTION_REGISTRY
    from lib.system_one_calibration import CalibrationBundle, CalibrationRecord

    records = []
    for question_id in QUESTION_REGISTRY:
        records.append(CalibrationRecord(
            question_id=question_id,
            backend_id=FIXTURE_BACKEND_ID,
            model_id=FIXTURE_MODEL_ID,
            model_revision=FIXTURE_MODEL_REVISION,
            model_content_hash=FIXTURE_MODEL_CONTENT_HASH,
            provider_protocol_hash=FIXTURE_PROVIDER_PROTOCOL_HASH,
            temperature=1.0,
            minimum_advisory_confidence=0.90,
            sample_count=100,
            training_count=80,
            validation_count=20,
            training_outcome_digest=digest(f"fixture-training:{question_id}"),
            validation_outcome_digest=digest(f"fixture-validation:{question_id}"),
            validation_accuracy=0.99,
            validation_brier=0.01,
            validation_ece=0.01,
            work_modes=("delivery",),
            deliverable_kinds=("code_behavior",),
            independent_trajectory_count=40,
            cohort_counts=(("delivery", "code_behavior", 40),),
            minimum_cohort_trajectories=20,
            minimum_outcomes=20,
            status="active"))
    return CalibrationBundle.active(records)


class FixtureSemanticBackend:
    """Injected test backend; the production provider is never patched."""

    backend_id = FIXTURE_BACKEND_ID
    model_id = FIXTURE_MODEL_ID
    model_revision = FIXTURE_MODEL_REVISION
    model_content_hash = FIXTURE_MODEL_CONTENT_HASH
    provider_protocol_hash = FIXTURE_PROVIDER_PROTOCOL_HASH
    max_latency_ms = 1000

    def __init__(self):
        from lib.protocol_artifact import (PROTOCOL_HASH, PROTOCOL_VERSION,
                                           QUESTION_REGISTRY, QUESTION_REGISTRY_HASH,
                                           STATE_PROJECTION_SCHEMA_HASH)
        from lib.system_one_contracts import SystemOneCapability

        self.route_options = []
        self._capability = SystemOneCapability(
            backend_id=self.backend_id,
            model_id=self.model_id,
            model_revision=self.model_revision,
            model_content_hash=self.model_content_hash,
            provider_protocol_hash=self.provider_protocol_hash,
            protocol_version=PROTOCOL_VERSION,
            protocol_hash=PROTOCOL_HASH,
            question_registry_hash=QUESTION_REGISTRY_HASH,
            state_projection_schema_hash=STATE_PROJECTION_SCHEMA_HASH,
            supported_question_families=tuple(QUESTION_REGISTRY),
            max_state_bytes=32768,
            stateful_sessions=False,
            max_concurrent_requests=1)

    def capability(self):
        return self._capability

    def reset_identity(self, identity):
        return None

    def resync(self, frame):
        return None

    def judge(self, frame):
        from lib.system_one_contracts import (QuestionAnswer, QuestionProbability,
                                              SystemOneJudgment)

        if "route_preference" in frame.question_ids:
            self.route_options.append(tuple(frame.state.legal_routes))
        preferred = {
            "action_relation": "advances_primary_goal",
            "progress_relation": "goal_progress",
            "retry_relation": "equivalent_retry",
            "goal_drift": "yes",
            "route_preference": "flow:tuid:validate",
        }
        answers = []
        for question in frame.questions:
            labels = tuple(label for label, _ in question.criteria)
            selected = preferred.get(question.question_id, labels[0])
            if selected not in labels:
                selected = labels[0]
            residual = 0.01 / max(1, len(labels) - 1)
            raw = tuple(QuestionProbability(
                label, 0.99 if label == selected else residual) for label in labels)
            answers.append(QuestionAnswer(
                question_id=question.question_id,
                selected=selected,
                raw_probabilities=raw))
        return SystemOneJudgment(
            identity=frame.identity,
            request_id=frame.request_id,
            sequence=frame.sequence,
            state_digest=frame.state_digest,
            question_bundle_digest=frame.question_bundle_digest,
            request_digest=frame.request_digest,
            policy_hash=frame.policy_hash,
            backend_id=frame.backend_id,
            model_id=frame.model_id,
            model_revision=frame.model_revision,
            model_content_hash=frame.model_content_hash,
            provider_protocol_hash=frame.provider_protocol_hash,
            calibration_bundle_hash=frame.calibration_bundle_hash,
            answers=tuple(answers))


class FixtureSemanticJudge:
    """Adapter from CFW's provider seam to DigitalPsychology's runtime."""

    def __init__(self, policy):
        from lib.system_one_runtime import SystemOneRuntime

        self.backend = FixtureSemanticBackend()
        self.calibrations = (fixture_calibration_bundle()
                             if policy.mode == "advisory" else None)
        expected_hash = self.calibrations.digest if self.calibrations else ""
        if policy.calibration_bundle_hash != expected_hash:
            raise AssertionError("CFW policy and DP fixture calibration hashes differ")
        self.system_one = SystemOneRuntime(
            self.backend, calibrations=self.calibrations, cache_size=0)
        self._capability = runtime.SystemOneCapability(
            backend_id=policy.backend_id,
            model_id=policy.model_id,
            model_revision=policy.model_revision,
            model_content_hash=policy.model_content_hash,
            provider_protocol_hash=policy.provider_protocol_hash,
            protocol_version=policy.protocol_version,
            protocol_hash=policy.protocol_hash,
            question_registry_hash=policy.question_registry_hash,
            state_projection_schema_hash=policy.state_projection_schema_hash,
            supported_question_families=policy.enabled_question_families,
            max_state_bytes=32768,
            stateful_sessions=False,
            max_concurrent_requests=1)

    @property
    def route_options(self):
        return self.backend.route_options

    def capability(self):
        return self._capability

    def reset_identity(self, identity):
        self.system_one.reset_identity(identity)

    def resync(self, frame):
        self.system_one.resync(frame)

    def judge(self, frame, *, mode):
        return self.system_one.judge(frame, mode=mode)


def system_one_policy(mode: str):
    if mode == "disabled":
        return runtime.SystemOnePolicy()
    calibration_hash = (fixture_calibration_bundle().digest
                        if mode == "advisory" else "")
    return runtime.SystemOnePolicy(
        mode=mode,
        backend_id="ablation-fixture",
        model_id="controlled-semantic-answers",
        model_revision="fixture-v1",
        model_content_hash=digest("ablation fixture model"),
        provider_protocol_hash=digest("ablation fixture protocol"),
        calibration_bundle_hash=calibration_hash,
        minimum_confidence_by_question={
            "action_relation": 0.90,
            "progress_relation": 0.90,
            "goal_drift": 0.90,
            "route_preference": 0.90,
        })


def goal_contract():
    return resolve.TaskGoalContract(
        objective="Deliver the requested behavior and validate it.",
        deliverable_kind="code_behavior",
        work_mode="delivery",
        acceptance_criteria=(resolve.AcceptanceCriterion(
            "C1", "The requested behavior is implemented.", "code_behavior"),))


def make_session(mode: str, *, task_id: str, subject: str,
                 domain_tags=(), operation=None, trigger=None,
                 requested_flow=None, action_registry=None, cow_root=COW):
    policy = system_one_policy(mode)
    request = resolve.TaskRequest(
        task_id=task_id,
        application_id="cfw-metacognitive-ablation",
        subject_ref=subject,
        shape="implement",
        domain_tags=tuple(domain_tags),
        operation=operation,
        trigger=trigger,
        goal_contract=goal_contract(),
        system_one_policy=policy,
        action_registry=action_registry or {})
    requested = ({"tuid": requested_flow} if requested_flow else None)
    bundle = resolve.resolve(
        request, requested_flows=requested, cow_root=cow_root,
        allow_missing_required=True)
    judge = None if mode == "disabled" else FixtureSemanticJudge(policy)
    session = runtime.start(
        bundle, behavioral_judge=judge, telemetry_disabled=True)
    return session, judge, request


def qualify_delivery_trace(mode: str) -> tuple[int, int, str, str]:
    subject = "workspace:ablation"
    registry = {"ambiguous-action": runtime.ActionDescriptor(
        "ambiguous-action", goal_relation="ambiguous", action_class="edit",
        affected_criterion_ids=("C1",))}
    session, judge, _ = make_session(
        mode, task_id="same-long-delivery-task", subject=subject,
        action_registry=registry)
    host = session._host_ingress()

    before = session.metacognitive_decision
    prepared = session.prepare_action(runtime.AgentActionRequest(
        binding_id="ambiguous-action", arguments={"target": "src/feature.py"},
        subject_ref=subject))
    assert prepared.decision == runtime.GateDecision.ALLOW
    after_proposal = session._evaluate_metacognition()
    assert after_proposal.mode == before.mode == "DELIVER"
    host.record_tool_result(
        "ambiguous-action", "success", subject,
        action_request_id=prepared.action_request_id,
        actual_result={"artifact_changed": False})

    # The same five support outcomes and one dispatched ambiguous action run
    # in A, B, and C. Laya's progress label is semantic telemetry only; the
    # support counter changes only through the host's action classification.
    for index in range(5):
        host.record_tool_result(
            "read_state", "success", subject,
            actual_result={"artifact_changed": False, "step": index})
    support = session.convergence.state("primary-objective").support_actions_since_goal_progress
    decision = session._evaluate_metacognition()
    assert support >= 5 and decision.mode == "REPLAN"
    assert session.metacognitive_state.criteria[0].evidence_status == "none"
    committed_before_evidence = session.metacognitive_state.committed_actions_since_goal_progress
    assert committed_before_evidence == 1

    # A fresh host observation explicitly bound to C1 resets the hard budget.
    evidence_ref = host.record_observation("rendered_observation", subject)
    host.record_goal_progress(
        cause_id="primary-objective", evidence_ref=evidence_ref, criterion_id="C1")
    assert session.convergence.state("primary-objective").support_actions_since_goal_progress == 0
    assert session.metacognitive_state.criteria[0].evidence_status == "available"

    for epoch in range(3):
        host.record_recovery_epoch(
            cause_id="primary-objective", reason=f"bounded recovery epoch {epoch + 1}")
    assert session.termination_disposition == "BLOCKED"
    assert session._evaluate_metacognition().mode == "BLOCKED"
    return support, committed_before_evidence, \
        after_proposal.mode, session.termination_disposition


def qualify_verification_budget(mode: str) -> tuple[str, ...]:
    subject = "workspace:verification"
    registry = {"required-check": runtime.ActionDescriptor(
        "required-check", goal_relation="required_verification",
        action_class="validate", affected_criterion_ids=("C1",))}
    session, _, _ = make_session(
        mode, task_id="same-verification-task", subject=subject,
        action_registry=registry)
    host = session._host_ingress()
    host.record_tool_result(
        "edit", "success", subject, actual_result={"artifact_changed": True})
    decisions = []
    for index in range(3):
        prepared = session.prepare_action(runtime.AgentActionRequest(
            binding_id="required-check",
            arguments={"target": "src/feature.py"}, subject_ref=subject))
        decisions.append(prepared.decision.value)
        if prepared.decision == runtime.GateDecision.ALLOW:
            host.record_tool_result(
                "required-check", "success", subject,
                action_request_id=prepared.action_request_id,
            actual_result={"artifact_changed": False, "check": index})
    assert decisions == ["allow", "allow", "block"]
    return tuple(decisions)


def prepare_route_root(temp_root: Path) -> None:
    shutil.copytree(COW, temp_root)
    discover = temp_root / "tuid" / "flows" / "discover" / "flow.md"
    discover.write_text(discover.read_text(encoding="utf-8").replace(
        "fallback: true", "fallback: false", 1), encoding="utf-8")
    fallback = temp_root / "tuid" / "flows" / "fallback" / "flow.md"
    fallback.parent.mkdir(parents=True)
    fallback.write_text(
        "---\nflow: fallback\noperations: [verify]\n"
        "triggers: [validation_requested]\nstates:\n"
        "  from: [IMPLEMENTED, EXERCISABLE, VALIDATED]\n"
        "  through: [VALIDATED]\nfallback: true\n"
        "required_specialists: []\noptional_specialists: []\n---\n"
        "# Lower-ranked fallback verification route\n", encoding="utf-8")


def qualify_routes(mode: str, cow_root: Path) -> tuple[str, str | None, tuple[str, ...]]:
    session, judge, request = make_session(
        mode, task_id="same-route-task", subject="ui:ablation",
        domain_tags=("terminal interface",), operation="verify",
        trigger="validation_requested", requested_flow="test", cow_root=cow_root)
    host = session._host_ingress()
    children = {
        f"flow:tuid:{flow}": resolve.resolve(
            request, requested_flows={"tuid": flow}, cow_root=cow_root,
            allow_missing_required=True)
        for flow in ("test", "validate", "fallback")
    }
    for index in range(5):
        host.record_tool_result(
            "inspect", "success", "ui:ablation",
            actual_result={"artifact_changed": False, "step": index})
    assert session._evaluate_metacognition().mode == "REPLAN"

    # Unique deterministic choice and epoch creation work with DP disabled.
    unique = host.rank_legal_routes(
        {"flow:tuid:validate": children["flow:tuid:validate"]},
        state_revision=0, target_statework_id="tuid")
    assert unique is not None and unique.route == "flow:tuid:validate"
    assert unique.source == "metacognition"
    epoch = host.create_child_policy_epoch(
        unique, selected_route=unique.route, current_state_revision=0)
    assert epoch.record.decision_source == "metacognition"
    runtime.start(epoch.bundle, behavioral_judge=judge,
                  telemetry_disabled=True, policy_epoch_record=epoch.record)

    # The two specific routes tie. The generic fallback is legal but lower
    # ranked, so it must never be exposed as a semantic choice.
    ambiguous = host.rank_legal_routes(
        children, state_revision=0, target_statework_id="tuid")
    expected_top = ("flow:tuid:test", "flow:tuid:validate")
    if mode == "advisory":
        assert ambiguous is not None and ambiguous.route == "flow:tuid:validate"
        assert ambiguous.source == "semantic_advisory"
        assert judge is not None and judge.route_options[-1] == expected_top
        assert ambiguous.route in judge.route_options[-1]
    else:
        assert ambiguous is None
        if judge is not None:
            assert judge.route_options == []
    return unique.source, ambiguous.source if ambiguous else None, \
        judge.route_options[-1] if judge and judge.route_options else ()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cfw-metacognitive-ablation-") as state_dir, \
            tempfile.TemporaryDirectory(prefix="cfw-route-fixture-") as route_dir:
        old_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = state_dir
        try:
            modes = ("disabled", "shadow", "advisory")
            delivery = {mode: qualify_delivery_trace(mode) for mode in modes}
            assert all(item[0] >= 5 and item[1] == 1 and item[2] == "DELIVER"
                       and item[3] == "BLOCKED" for item in delivery.values())
            print("A/B/C delivery trace: hard progress and recovery limits preserved")

            verification = {mode: qualify_verification_budget(mode) for mode in modes}
            assert verification == {mode: ("allow", "allow", "block") for mode in modes}
            print("A/B/C verification trace: repeated unchanged checks stop identically")

            route_root = Path(route_dir) / "CognitiveStateWork"
            prepare_route_root(route_root)
            routes = {mode: qualify_routes(mode, route_root) for mode in modes}
            assert all(routes[mode][0] == "metacognition" for mode in modes)
            assert routes["disabled"][1] is None and routes["shadow"][1] is None
            assert routes["advisory"][1] == "semantic_advisory"
            assert routes["advisory"][2] == ("flow:tuid:test", "flow:tuid:validate")
            print("A/B/C route trace: deterministic unique route works without DP; "
                  "advisory resolves only the tied top routes")
        finally:
            if old_runtime_dir is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = old_runtime_dir
    print("METACOGNITIVE_ABLATION=PASS (DigitalPsychology runtime with controlled "
          "backend/calibration fixtures; no model qualification)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
