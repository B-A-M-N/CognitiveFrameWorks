from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_contract.actions import ActionDescriptor
from runtime_contract.evidence import CompletionClaim
from runtime_contract.routing import comparison_context_hash
from runtime_contract.standalone_attention import StandaloneAttentionPolicy
from runtime_contract.task import (
    AcceptanceCriterion,
    TaskGoalContract,
    TaskRequest,
    VerificationPolicy,
    task_goal_contract_mapping_hash,
)


def _modules():
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


def test_goal_contract_is_pinned_in_policy_manifest_and_model_context():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Change the buddy's visual composition while keeping activity readable.",
        deliverable_kind="visual_ui",
        work_mode="delivery",
        acceptance_criteria=(
            AcceptanceCriterion("C1", "Visual composition changed", "visual_static"),
            AcceptanceCriterion("C2", "Activity remains readable", "legibility"),
        ),
        non_goals=("Do not redesign unrelated panels",),
    )
    request = TaskRequest(task_id="goal-fidelity", application_id="test",
                          shape="implement", goal_contract=goal)
    bundle = resolve.resolve(request)
    task_manifest = resolve.build_task_manifest(bundle)
    session = runtime.start(bundle, session_nonce="goal-fidelity", telemetry_disabled=True)
    context = runtime.context_for(session)

    assert bundle.policy.goal_contract_hash == goal.contract_hash
    assert bundle.pinned["goal_contract_hash"] == goal.contract_hash
    assert task_manifest["goal_contract_hash"] == goal.contract_hash
    assert task_manifest["work_mode"] == "delivery"
    assert context.startswith("===== TASK CONTRACT =====")
    assert "Change the buddy's visual composition" in context
    assert "C1: Visual composition changed" in context
    assert "Do not redesign unrelated panels" in context
    assert context.index("TASK CONTRACT") < context.index("ACTIVE PIPELINE")


def test_component_visibility_hides_enforcement_only_stage_identity():
    resolve, runtime = _modules()
    session = runtime.start(resolve.resolve("visibility-contract", "implement"),
                            session_nonce="visibility-contract", telemetry_disabled=True)
    session.lifecycle_workflow.active_lifecycle.update({"owl", "ward", "sispis"})
    session.injected["ward"] = "WARD private enforcement capsule"
    session.injected["sispis"] = "SISPIS output-only capsule"

    context = runtime.context_for(session)

    visible_pipeline = context.split("ACTIVE PIPELINE:", 1)[1].splitlines()[0]
    assert "ward" not in visible_pipeline
    assert "sispis" not in visible_pipeline
    assert "WARD private enforcement capsule" not in context
    assert "SISPIS output-only capsule" not in context


def test_default_action_strategy_does_not_claim_an_ordering():
    resolve, runtime = _modules()
    request = TaskRequest(task_id="visual-delivery", application_id="test",
                          shape="implement", goal_contract=TaskGoalContract(
                              objective="Change the component appearance.",
                              deliverable_kind="visual_ui", work_mode="delivery",
                              acceptance_criteria=(AcceptanceCriterion(
                                  "C1", "The component appearance changes.",
                                  "static_visual"),)))
    session = runtime.start(resolve.resolve(request), session_nonce="visual-delivery",
                            telemetry_disabled=True)
    assert session._action_strategy()["priorities"] == {}
    assert session._action_strategy_hint() == ""
    assert "ACTION STRATEGY" not in runtime.context_for(session)


def test_tool_capability_does_not_activate_action_wrappers_but_planning_does():
    resolve, runtime = _modules()
    bundle = resolve.resolve("lifecycle-intent", "implement")
    session = runtime.start(bundle, session_nonce="lifecycle-intent", telemetry_disabled=True)

    session.before_model_call(tool_capable=True)
    assert "fuse" not in session.lifecycle_workflow.lifecycle.active
    assert "ward" not in session.lifecycle_workflow.lifecycle.active

    session.before_tool_planning({"tool_binding_id": "delete", "arguments": {}})
    assert {"fuse", "ward"}.issubset(session.lifecycle_workflow.lifecycle.active)
    session._host_ingress().record_tool_result("read", "success", invocation_id="read-1")
    assert "fuse" not in session.lifecycle_workflow.lifecycle.active
    assert "ward" not in session.lifecycle_workflow.lifecycle.active
    assert "fuse" not in session.lifecycle_workflow.lifecycle.task_active
    assert "ward" not in session.lifecycle_workflow.lifecycle.task_active


def test_artifact_and_response_stages_expire_at_their_boundaries():
    resolve, runtime = _modules()
    bundle = resolve.resolve("lifecycle-artifact", "doc_edit")
    session = runtime.start(bundle, session_nonce="lifecycle-artifact", telemetry_disabled=True)

    session.on_artifact(durable_contract_changed=True, state_changed=True)
    assert {"dox_closeout", "anchor_closeout"}.issubset(session.lifecycle_workflow.lifecycle.active)
    session.artifact_processed()
    assert "dox_closeout" not in session.lifecycle_workflow.lifecycle.active
    assert "anchor_closeout" not in session.lifecycle_workflow.lifecycle.active

    session._render_output({}, complete=True)
    assert "sispis" not in session.lifecycle_workflow.lifecycle.active
    assert "sispis" not in session.lifecycle_workflow.lifecycle.task_active


def test_task_nontrivial_predicate_uses_trusted_complexity():
    assert not runtime_predicate("task.nontrivial", "trivial")
    assert runtime_predicate("task.nontrivial", "standard")
    assert runtime_predicate("task.nontrivial", "complex")


def runtime_predicate(predicate: str, complexity: str) -> bool:
    _resolve, runtime = _modules()
    return runtime.LifecycleController._predicate_matches(
        predicate, {"task_complexity": complexity})


def test_goal_contract_survives_resume():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Make the requested visual edit.",
        deliverable_kind="visual_ui",
        work_mode="delivery",
        acceptance_criteria=(
            AcceptanceCriterion("C1", "Visual composition changed", "static_visual"),
        ),
    )
    bundle = resolve.resolve(TaskRequest(
        task_id="goal-resume", application_id="test", shape="implement",
        goal_contract=goal))
    runtime.start(bundle, session_nonce="goal-resume", telemetry_disabled=True)
    resumed = runtime.resume_task(bundle, "goal-resume", telemetry_disabled=True)
    context = runtime.context_for(resumed)
    assert goal.contract_hash not in context
    assert "Visual composition changed" in context


def test_generic_layout_does_not_select_tuid_but_terminal_layout_does():
    resolve, _runtime = _modules()
    stateworks = resolve.load_stateworks(resolve.DEFAULT_STATEWORK_ROOT)
    selected, ambiguous = resolve.match_statework(stateworks, ["layout"])
    assert selected is None
    assert ambiguous == []
    selected, ambiguous = resolve.match_statework(
        stateworks, ["terminal UI layout"])
    assert selected["id"] == "tuid"
    assert ambiguous == []


def test_goal_comparison_context_keeps_dimensions_without_exact_contract_hash():
    context = {
        "task_shape": "implement", "domain_tags": ["ui"],
        "work_mode": "delivery", "deliverable_kind": "visual_ui",
        "acceptance_criterion_kinds": ["static_visual"],
        "verification_policy": {"support_actions_before_goal_reanchor": 5},
        "policy_hash": "one-task-policy", "goal_contract_hash": "one-goal",
    }
    equivalent = {
        **context, "policy_hash": "another-task-policy",
        "goal_contract_hash": "another-goal",
    }
    assert comparison_context_hash(context) == comparison_context_hash(equivalent)


def test_tuid_implement_task_cannot_select_test_flow():
    resolve, _runtime = _modules()
    stateworks = resolve.load_stateworks(resolve.DEFAULT_STATEWORK_ROOT)
    tuid = next(item for item in stateworks if item["id"] == "tuid")
    implement, _ = resolve.select_statework_runtime(
        tuid, resolve.DEFAULT_STATEWORK_ROOT, operation="implement", trigger="mutation",
        task_shape="implement", current_state="DESIGNED")
    test, _ = resolve.select_statework_runtime(
        tuid, resolve.DEFAULT_STATEWORK_ROOT, operation="test", trigger="test_requested",
        task_shape="test", current_state="IMPLEMENTED")
    assert implement["flow_metadata"]["flow"] == "implement"
    assert test["flow_metadata"]["flow"] == "test"
    selector = resolve._flow_selector(resolve.DEFAULT_STATEWORK_ROOT)
    context = {"operation": "test", "trigger": "test_requested",
               "task_shape": "test", "current_state": "IMPLEMENTED"}
    assert not selector.evaluate(implement["flow_metadata"]["when"], context)[0]
    assert selector.evaluate(test["flow_metadata"]["when"], context)[0]


def test_tuid_completion_is_claim_sensitive(tmp_path):
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Change the static visual composition.",
        deliverable_kind="visual_ui", work_mode="delivery",
        acceptance_criteria=(
            AcceptanceCriterion("C1", "The static visual changes", "static_visual"),
        ))
    bundle = resolve.resolve(TaskRequest(
        task_id="visual-claim", application_id="test", shape="implement",
        domain_tags=("terminal UI",), operation="implement", trigger="mutation",
        subject_ref="component:buddy", requested_statework_ids=("tuid",),
        goal_contract=goal))
    authority = runtime.create_state_authority(
        resolve.DEFAULT_STATEWORK_ROOT, state_path=tmp_path / "subjects.json",
        evidence_path=tmp_path / "evidence.json")
    session = runtime.start(bundle, telemetry_disabled=True,
                            state_authority=authority,
                            session_nonce="visual-claim")
    assert session._criterion_evidence_kinds("static_visual") == {"rendered_observation"}
    assert session._criterion_evidence_kinds("interaction") == {"interaction_validation"}
    assert session._criterion_evidence_kinds("terminal_lifecycle") == {"pty_validation"}
    assert session._criterion_evidence_kinds("code_behavior") == {"tool_result"}
    workflow_completion = session.bundle.pinned[
        "workflow_completion_claim_classes"]["tuid"]["static_visual"]
    assert workflow_completion["minimum_state"] == "IMPLEMENTED"
    assert set(workflow_completion["evidence_kinds"]) == {"rendered_observation"}
    flow = next(item["flow_metadata"] for item in bundle.stateworks
                if item["id"] == "tuid")
    assert flow["completion"]["claim_classes"]["static_visual"]["minimum_state"] == "IMPLEMENTED"


def test_verification_progress_does_not_reset_goal_drift_budget():
    from runtime_contract.convergence import ConvergenceController

    controller = ConvergenceController(identity={
        "namespace_id": "ns", "application_instance_id": "app",
        "agent_instance_id": "agent", "session_id": "session",
        "task_id": "goal-drift", "attempt_id": "attempt",
    })
    controller.record_support_action(cause_id="primary-objective")
    controller.record_support_action(cause_id="primary-objective")
    controller.record_progress(
        cause_id="primary-objective", progress_digest="tests-passed",
        progress_class="VERIFICATION_PROGRESS")
    state = controller.state("primary-objective")
    assert state.support_actions_since_goal_progress == 3
    controller.record_progress(
        cause_id="primary-objective", progress_digest="artifact-changed",
        progress_class="GOAL_PROGRESS")
    assert controller.state("primary-objective").support_actions_since_goal_progress == 0


def test_host_retry_contract_uses_cause_and_plan_not_tool_name():
    resolve, runtime = _modules()
    bundle = resolve.resolve("attempt-contract", "quick")
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="attempt-contract")
    host = session._host_ingress()
    failure_evidence = host.record_observation("diagnostic_observation")
    host.begin_attempt(attempt_ref="a1", cause_id="layout-cause",
                       premise_digest="premise-1",
                       action_plan={"tool": "inspect", "target": "component"})
    host.record_attempt_failure(attempt_ref="a1", evidence_ref=failure_evidence)
    assert session.before_retry({
        "tool_binding_id": "inspect", "arguments": {"attempt_ref": "a1"}}) == (
            runtime.GateDecision.REQUIRE_REINSPECTION)
    host.record_reinspection(cause_id="layout-cause", revision=1)
    assert session.before_retry({
        "tool_binding_id": "inspect", "arguments": {"attempt_ref": "a1"}}) == (
            runtime.GateDecision.REQUIRE_CHANGED_HYPOTHESIS)
    host.begin_attempt(attempt_ref="a2", cause_id="layout-cause",
                       premise_digest="premise-2",
                       action_plan={"tool": "inspect", "target": "component"})
    assert session.before_retry({
        "tool_binding_id": "inspect", "arguments": {"attempt_ref": "a2"}}) == (
            runtime.GateDecision.ALLOW)
    assert len(session.attempts.controller.states()) == 1


def test_recovery_exhaustion_issues_terminal_disposition():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Diagnose the blocked task.", deliverable_kind="analysis",
        work_mode="diagnose",
        acceptance_criteria=(AcceptanceCriterion("C1", "Blocker identified", "diagnosis"),),
        verification_policy=VerificationPolicy(max_recovery_epochs_without_goal_progress=1))
    session = runtime.start(resolve.resolve(TaskRequest(
        task_id="recovery-exhaustion", application_id="test",
        shape="quick", goal_contract=goal)),
        telemetry_disabled=True, session_nonce="recovery-exhaustion")
    host = session._host_ingress()
    host.record_recovery_epoch(cause_id="cause-1", reason="No safe recovery remains.")
    assert session.lifecycle_workflow.termination_disposition == "BLOCKED"
    result = session.finish_task_terminated()
    assert result["status"] == "blocked"
    assert result["completed"] is False


def test_successful_completion_requires_a_nonempty_goal_contract():
    resolve, runtime = _modules()
    session = runtime.start(resolve.resolve("completion-without-goal", "quick"),
                            telemetry_disabled=True)
    claim = CompletionClaim.create(
        claim_id="unscoped-success", task_id=session.bundle.task_id,
        subject_ref=session.bundle.policy.subject_ref, boundary_type="completion",
        boundary_target="task", expected_validator="completion-boundary-v1",
        invocation_id="completion")
    assert session.record_completion_claim(
        session.bundle.policy.subject_ref, [], claim=claim) == (
            runtime.GateDecision.REQUIRE_REINSPECTION)


def test_metacognitive_readiness_waits_for_host_attested_boundary_evidence():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Produce and verify the requested behavior.",
        deliverable_kind="code_behavior", work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "The requested behavior is present.", "code_behavior"),))
    bundle = resolve.resolve(TaskRequest(
        task_id="completion-readiness", application_id="test", shape="quick",
        subject_ref="repo:readiness", goal_contract=goal))
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="completion-readiness")
    assert session.behavior.authority is None
    session.behavior.metacognitive.mark_criterion_satisfied(
        "C1", evidence_ref="criterion-proof", evidence_kind="validator_result")
    assert session._evaluate_metacognition().mode == "VERIFY"
    assert session.behavior.metacognitive_controller is not None
    with pytest.raises(RuntimeError, match="standalone behavior services"):
        session.enable_external_behavior_authority()

    host = session._host_ingress()
    host.record_tool_result("test", "pass", "repo:readiness",
                            invocation_id="boundary-invocation")
    claim = CompletionClaim.create(
        claim_id="readiness-claim", task_id=bundle.task_id,
        subject_ref="repo:readiness", boundary_type="completion",
        boundary_target="task", expected_validator="completion-boundary-v1",
        invocation_id="boundary-invocation", goal_contract_hash=goal.contract_hash,
        criterion_ids=("C1",))
    boundary = host.record_validator_result(
        "completion-boundary-v1", "boundary-invocation", {"status": "pass"},
        "repo:readiness", claim_digest=claim.claim_digest)
    assert session.evidence.ledger.get(boundary) is not None
    assert session._evaluate_metacognition().mode == "READY_TO_COMPLETE"


def test_completion_requires_explicit_evidence_coverage_for_affected_boundaries():
    resolve, runtime = _modules()
    subject = "repo:boundary-coverage"
    goal = TaskGoalContract(
        objective="Refactor authentication without changing its guarantees.",
        deliverable_kind="code_behavior", work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "Authentication behavior remains correct", "code_behavior",
            required_boundaries=("token_parser", "permission_boundary")),))
    bundle = resolve.resolve(TaskRequest(
        task_id="boundary-coverage", application_id="test", shape="implement",
        subject_ref=subject, goal_contract=goal))
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="boundary-coverage")
    host = session._host_ingress()

    def submit_completion(invocation_id: str, covered: tuple[str, ...]):
        proof = host.record_tool_result(
            "test", "pass", subject, invocation_id=invocation_id,
            covered_boundary_ids=covered)
        claim = CompletionClaim.create(
            claim_id=f"claim-{invocation_id}", task_id=bundle.task_id,
            subject_ref=subject, boundary_type="completion",
            boundary_target="task", expected_validator="completion-boundary-v1",
            invocation_id=invocation_id, goal_contract_hash=goal.contract_hash,
            criterion_ids=("C1",), criterion_evidence_refs={"C1": (proof,)})
        boundary = host.record_validator_result(
            "completion-boundary-v1", invocation_id, {"status": "pass"},
            subject, claim_digest=claim.claim_digest)
        return session.record_completion_claim(subject, [boundary], claim=claim)

    assert submit_completion("partial-coverage", ("token_parser",)) == (
        runtime.GateDecision.REQUIRE_REINSPECTION)
    assert submit_completion(
        "complete-coverage", ("token_parser", "permission_boundary")) == (
            runtime.GateDecision.ALLOW)
    assert session._evaluate_metacognition().goal_reanchor_required is False


def test_required_boundary_identifiers_are_bounded_and_unique():
    with pytest.raises(ValueError, match="required_boundaries"):
        AcceptanceCriterion("C1", "claim", "code_behavior",
                           required_boundaries=("auth", "auth"))
    with pytest.raises(ValueError, match="required_boundaries"):
        AcceptanceCriterion("C1", "claim", "code_behavior",
                           required_boundaries=tuple(f"b-{i}" for i in range(33)))


def test_evidence_boundary_coverage_survives_session_resume(tmp_path, monkeypatch):
    resolve, runtime = _modules()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    bundle = resolve.resolve(TaskRequest(
        task_id="boundary-coverage-resume", application_id="test",
        shape="implement", subject_ref="repo:boundary-resume"))
    identity = {"session_nonce": "boundary-coverage-resume",
                "session_id": "session-boundary-resume"}
    session = runtime.start(bundle, telemetry_disabled=True, **identity)
    evidence_ref = session._host_ingress().record_tool_result(
        "test", "pass", "repo:boundary-resume", invocation_id="resume-test",
        covered_boundary_ids=("token_parser", "permission_boundary"))

    resumed = runtime.start(bundle, telemetry_disabled=True, resume=True, **identity)

    assert resumed.identity_authority.identity == session.identity_authority.identity
    assert resumed.lifecycle_workflow.current_segment == session.lifecycle_workflow.current_segment
    assert resumed.evidence.ledger.covered_boundary_ids(
        (evidence_ref,), subject_ref="repo:boundary-resume") == (
            "permission_boundary", "token_parser")


def test_runtime_session_state_has_typed_subsystem_owners():
    resolve, runtime = _modules()
    session = runtime.start(resolve.resolve("aggregate-boundaries", "quick"),
                            telemetry_disabled=True,
                            session_nonce="aggregate-boundaries")

    assert session.identity_authority.identity.session_id
    assert session.lifecycle_workflow.lifecycle is not None
    assert session.actions.action_registry
    assert session.evidence.ledger.task_id == session.bundle.task_id
    assert session.attempts.controller is not None
    assert session.behavior.metacognitive is not None
    assert session.telemetry_state.journal_path is not None
    for legacy_field in ("session_id", "state_authority", "lifecycle", "ledger",
                         "attempt_descriptors", "behavior_authority", "journal_path"):
        assert not hasattr(session, legacy_field)


def test_started_session_freezes_identity_authority_and_aggregate_bindings():
    from dataclasses import FrozenInstanceError

    resolve, runtime = _modules()
    session = runtime.start(resolve.resolve("aggregate-freeze", "quick"),
                            telemetry_disabled=True,
                            session_nonce="aggregate-freeze")

    with pytest.raises(FrozenInstanceError):
        session.identity_authority.identity.session_id = "rebound"
    with pytest.raises(FrozenInstanceError):
        session.identity_authority.authorities.runtime_mode = "combined_enforcing"
    with pytest.raises(AttributeError, match="coordinator is sealed"):
        session.identity_authority = session.identity_authority


def test_validator_evidence_does_not_count_as_support_action():
    resolve, runtime = _modules()
    session = runtime.start(resolve.resolve("validator-accounting", "quick"),
                            telemetry_disabled=True,
                            session_nonce="validator-accounting")
    host = session._host_ingress()
    host.record_tool_result("test", "pass", "repo:validator",
                            invocation_id="validator-invocation",
                            actual_result={"fresh": True, "status": "pass"})
    host.record_validator_result(
        "reinspection-v1", "validator-invocation",
        {"fresh": True, "status": "pass"}, "repo:validator")
    state = session.attempts.controller.state("primary-objective")
    assert state.support_actions_since_goal_progress == 0
    assert state.evidence_facts_since_goal_progress == 1


def test_twenty_turn_delivery_scenario_redirects_support_without_reanchoring():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Implement the requested component behavior.",
        deliverable_kind="code_behavior", work_mode="delivery",
        acceptance_criteria=(
            AcceptanceCriterion("C1", "Component behavior matches the request", "code_behavior"),
        ),
        verification_policy=VerificationPolicy(
            support_actions_before_goal_reanchor=5))
    bundle = resolve.resolve(TaskRequest(
        task_id="hostile-visual-loop", application_id="test", shape="implement",
        goal_contract=goal))
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="hostile-visual-loop")
    host = session._host_ingress()

    # Mutate after one narrow inspection; a green test run is not goal progress.
    assert session.before_action({"tool_binding_id": "inspect", "arguments": {}}) == runtime.GateDecision.ALLOW
    host.record_tool_result("inspect", "success", invocation_id="inspect-1")
    write_action = {"tool_binding_id": "write", "arguments": {"target": "component.py"}}
    binding = host.bind_action_objective(
        write_action, purpose_class="DIRECT_GOAL", criterion_ids=("C1",))
    prepared = host.prepare_action(write_action, objective_binding=binding)
    assert prepared.decision == runtime.GateDecision.ALLOW
    changed = host.record_tool_result(
        "write", "success", invocation_id="write-1",
        action_request_id=prepared.action_request_id,
        actual_result={"artifact_changed": True})
    host.record_goal_progress(cause_id="primary-objective", evidence_ref=changed,
                              criterion_id="C1")

    observed_redirect = False
    for turn in range(24):
        context = session.before_model_call(tool_capable=True)
        assert goal.objective in context
        assert "fuse" not in session.lifecycle_workflow.lifecycle.active
        assert "ACTION STRATEGY" not in context
        result = session.before_action({
            "tool_binding_id": "test", "arguments": {"turn": turn}})
        if result == runtime.GateDecision.REQUIRE_CHANGED_HYPOTHESIS:
            observed_redirect = True
            context = runtime.context_for(session)
            assert "PROGRESS REDIRECT REQUIRED" in context
            assert "GOAL REANCHOR REQUIRED" not in context
            break
        assert result == runtime.GateDecision.ALLOW
        host.record_tool_result("test", "success", invocation_id=f"test-{turn}")
        assert "fuse" not in session.lifecycle_workflow.lifecycle.active
        assert "ward" not in session.lifecycle_workflow.lifecycle.active
    assert observed_redirect


def test_progress_redirect_is_not_a_persistent_gate_and_yields_to_another_criterion():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Implement the requested behavior.", deliverable_kind="code_behavior",
        work_mode="delivery",
        acceptance_criteria=tuple(
            AcceptanceCriterion(item, f"Required behavior {item}", "code_behavior")
            for item in ("C1", "C2", "C3")),
        verification_policy=VerificationPolicy(
            support_actions_before_goal_reanchor=1,
            max_committed_actions_without_goal_progress=1))
    bundle = resolve.resolve(TaskRequest(
        task_id="bounded-progress-redirect", application_id="test", shape="implement",
        goal_contract=goal,
        action_registry={"inspect": ActionDescriptor("inspect")}))
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="bounded-progress-redirect")
    host = session._host_ingress()

    first = host.prepare_action({
        "tool_binding_id": "inspect", "arguments": {"target": "first.py"}})
    assert first.decision == runtime.GateDecision.ALLOW
    host.record_tool_result(
        "inspect", "success", invocation_id="inspect-first",
        action_request_id=first.action_request_id,
        actual_result={"artifact_changed": False})

    redirected_action = {
        "tool_binding_id": "inspect", "arguments": {"target": "second.py"}}
    redirected = host.prepare_action(redirected_action)
    assert redirected.decision == runtime.GateDecision.REQUIRE_CHANGED_HYPOTHESIS
    context = runtime.context_for(session)
    assert "PROGRESS REDIRECT REQUIRED" in context
    assert "GOAL REANCHOR REQUIRED" not in context
    assert "PROGRESS REDIRECT REQUIRED" not in runtime.context_for(session)
    assert session._request(redirected_action).digest == \
        session.behavior.metacognitive._last_progress_redirect_action
    assert session.behavior.metacognitive._last_progress_redirect_criterion == "C1"

    # The same request is blocked without repeating the redirect instruction.
    repeated = host.prepare_action(redirected_action)
    assert repeated.decision == runtime.GateDecision.BLOCK
    assert repeated.reason_codes == ("flow.progress_redirect_duplicate_action",)

    # A distinct attempt rotates attention and proceeds.
    next_action = {
        "tool_binding_id": "inspect", "arguments": {"target": "third.py"}}
    yielded = host.prepare_action(next_action)
    assert yielded.decision == runtime.GateDecision.ALLOW
    host.record_tool_result(
        "inspect", "success", invocation_id="inspect-third",
        action_request_id=yielded.action_request_id,
        actual_result={"artifact_changed": False})

    # The first action for the yielded-to criterion is permitted once.
    next_yielded = host.prepare_action({
        "tool_binding_id": "inspect", "arguments": {"target": "fourth.py"}})
    assert next_yielded.decision == runtime.GateDecision.ALLOW
    assert session.behavior.metacognitive.active_attention_window()[0] == "C2"
    assert "Current focus: C2" in runtime.context_for(session)


def test_standalone_attention_yields_from_repeatedly_stuck_criterion():
    criteria = (
        SimpleNamespace(criterion_id="C1", status="in_progress"),
        SimpleNamespace(criterion_id="C2", status="pending"),
        SimpleNamespace(criterion_id="C3", status="pending"),
    )

    focus = StandaloneAttentionPolicy.select(
        criteria, retry_counts={"C1": 2},
        last_action_sequence={"C1": 9, "C2": 0, "C3": 0})

    assert focus == ("C2", "C3", "C1")


def test_host_objective_binding_is_request_specific_and_resets_only_on_evidence():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Implement the requested behavior.", deliverable_kind="code_behavior",
        work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion("C1", "Requested behavior exists", "code_behavior"),))
    bundle = resolve.resolve(TaskRequest(
        task_id="objective-binding", application_id="test", shape="implement",
        goal_contract=goal))
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="objective-binding")
    host = session._host_ingress()
    action = {"tool_binding_id": "read", "arguments": {"target": "src/feature.py"}}
    binding = host.bind_action_objective(
        action, purpose_class="DIRECT_GOAL", criterion_ids=("C1",))
    prepared = host.prepare_action(action, objective_binding=binding)
    assert prepared.decision == runtime.GateDecision.ALLOW
    pending = session.actions.pending_action_requests[prepared.action_request_id]
    assert pending["relation"] == "advances_primary_goal"
    assert pending["accounting_lane"] == "DIRECT"
    evidence = host.record_tool_result(
        "read", "success", action_request_id=prepared.action_request_id,
        actual_result={"artifact_changed": True})
    state = session.attempts.controller.state("primary-objective")
    assert state.total_actions_since_goal_progress == 1
    assert state.support_actions_since_goal_progress == 0
    host.record_goal_progress(cause_id="primary-objective", evidence_ref=evidence,
                              criterion_id="C1")
    state = session.attempts.controller.state("primary-objective")
    assert state.total_actions_since_goal_progress == 0
    assert state.support_actions_since_goal_progress == 0


def test_standalone_host_descriptor_keeps_validator_relation_without_objective_binding():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Implement the requested behavior.", deliverable_kind="code_behavior",
        work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "Requested behavior exists", "code_behavior"),),
        verification_policy=VerificationPolicy(
            support_actions_before_goal_reanchor=1,
            max_committed_actions_without_goal_progress=1))
    action_registry = {
        "read": ActionDescriptor("read"),
        "host_validator": ActionDescriptor(
            "host_validator", goal_relation="required_verification"),
    }
    bundle = resolve.resolve(TaskRequest(
        task_id="standalone-validator-anchor", application_id="test",
        shape="implement", goal_contract=goal,
        action_registry=action_registry))
    session = runtime.start(bundle, telemetry_disabled=True)
    host = session._host_ingress()

    read_action = {"tool_binding_id": "read", "arguments": {"target": "src/feature.py"}}
    read = host.prepare_action(read_action)
    assert read.decision == runtime.GateDecision.ALLOW
    host.record_tool_result(
        "read", "success", action_request_id=read.action_request_id,
        actual_result={"inspected": True})

    validator_action = {
        "tool_binding_id": "host_validator",
        "arguments": {"validator": "feature"},
    }
    prepared = host.prepare_action(validator_action)

    assert prepared.decision == runtime.GateDecision.ALLOW
    pending = session.actions.pending_action_requests[prepared.action_request_id]
    assert pending["relation"] == "required_verification"


def test_goal_contract_mapping_hash_detects_explicit_contract_drift():
    goal = TaskGoalContract(
        objective="Implement the requested behavior.", deliverable_kind="code_behavior",
        work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "Requested behavior exists", "code_behavior"),))

    assert task_goal_contract_mapping_hash(goal.to_dict()) == goal.contract_hash
    changed = {**goal.to_dict(), "objective": "Implement a different behavior."}
    assert task_goal_contract_mapping_hash(changed) != goal.contract_hash


def test_invalid_goal_contract_identity_is_the_reanchor_condition():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Implement the requested behavior.", deliverable_kind="code_behavior",
        work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion(
            "C1", "Requested behavior exists", "code_behavior"),))
    session = runtime.start(resolve.resolve(TaskRequest(
        task_id="goal-anchor-drift", application_id="test", shape="implement",
        goal_contract=goal)), telemetry_disabled=True)
    session._evaluate_metacognition()
    controller = session.behavior.metacognitive_controller
    state = session.behavior.metacognitive
    decision = controller.evaluate(
        state, support_actions_since_progress=0, support_action_limit=5,
        total_actions_since_progress=0, total_action_limit=15,
        recovery_epochs_since_progress=0, max_recovery_epochs=3,
        same_approach_retries=0, max_same_approach_retries=2,
        goal_contract_anchor_valid=False)

    assert decision.goal_reanchor_required is True


def test_model_supplied_objective_claim_is_unclassified_and_not_support():
    resolve, runtime = _modules()
    goal = TaskGoalContract(
        objective="Implement the requested behavior.", deliverable_kind="code_behavior",
        work_mode="delivery",
        acceptance_criteria=(AcceptanceCriterion("C1", "Requested behavior exists", "behavior"),))
    bundle = resolve.resolve(TaskRequest(
        task_id="unbound-objective", application_id="test", shape="implement",
        goal_contract=goal))
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="unbound-objective")
    host = session._host_ingress()
    action = {"tool_binding_id": "read", "arguments": {"target": "src/feature.py"},
              "objective_binding": {"purpose_class": "DIRECT_GOAL", "criterion_ids": ["C1"]}}
    prepared = session.prepare_action(action)
    assert prepared.decision == runtime.GateDecision.ALLOW
    pending = session.actions.pending_action_requests[prepared.action_request_id]
    assert pending["relation"] == "ambiguous"
    assert pending["accounting_lane"] == "UNCLASSIFIED"
    host.record_tool_result("read", "success", action_request_id=prepared.action_request_id,
                            actual_result={"artifact_changed": False})
    state = session.attempts.controller.state("primary-objective")
    assert state.total_actions_since_goal_progress == 1
    assert state.support_actions_since_goal_progress == 0
    assert session.attempts.controller.state("primary-objective").last_goal_progress_seq == 0

    claim = CompletionClaim.create(
        claim_id="green-tests-only", task_id=bundle.task_id,
        subject_ref=bundle.policy.subject_ref, boundary_type="completion",
        boundary_target="task", expected_validator="completion-boundary-v1",
        invocation_id="tests", goal_contract_hash=goal.contract_hash,
        criterion_ids=("C1",))
    assert session.record_completion_claim(
        bundle.policy.subject_ref, [], claim=claim) == runtime.GateDecision.REQUIRE_REINSPECTION
