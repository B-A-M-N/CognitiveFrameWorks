from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_contract.task import (AcceptanceCriterion, TaskGoalContract,
                                  TaskRequest, VerificationPolicy)
from runtime_contract.evidence import CompletionClaim
from runtime_contract.routing import comparison_context_hash


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
    assert "fuse" not in session.lifecycle.active
    assert "ward" not in session.lifecycle.active

    session.before_tool_planning({"tool_binding_id": "delete", "arguments": {}})
    assert {"fuse", "ward"}.issubset(session.lifecycle.active)
    session._host_ingress().record_tool_result("read", "success", invocation_id="read-1")
    assert "fuse" not in session.lifecycle.active
    assert "ward" not in session.lifecycle.active
    assert "fuse" not in session.lifecycle.task_active
    assert "ward" not in session.lifecycle.task_active


def test_artifact_and_response_stages_expire_at_their_boundaries():
    resolve, runtime = _modules()
    bundle = resolve.resolve("lifecycle-artifact", "doc_edit")
    session = runtime.start(bundle, session_nonce="lifecycle-artifact", telemetry_disabled=True)

    session.on_artifact(durable_contract_changed=True, state_changed=True)
    assert {"dox_closeout", "anchor_closeout"}.issubset(session.lifecycle.active)
    session.artifact_processed()
    assert "dox_closeout" not in session.lifecycle.active
    assert "anchor_closeout" not in session.lifecycle.active

    session._render_output({}, complete=True)
    assert "sispis" not in session.lifecycle.active
    assert "sispis" not in session.lifecycle.task_active


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
    assert goal.contract_hash in context
    assert "Visual composition changed" in context


def test_generic_layout_does_not_select_tuid_but_terminal_layout_does():
    resolve, _runtime = _modules()
    stateworks = resolve.load_stateworks(resolve.COW_ROOT)
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
    stateworks = resolve.load_stateworks(resolve.COW_ROOT)
    tuid = next(item for item in stateworks if item["id"] == "tuid")
    implement, _ = resolve.select_statework_runtime(
        tuid, resolve.COW_ROOT, operation="implement", trigger="mutation",
        task_shape="implement", current_state="DESIGNED")
    test, _ = resolve.select_statework_runtime(
        tuid, resolve.COW_ROOT, operation="test", trigger="test_requested",
        task_shape="test", current_state="IMPLEMENTED")
    assert implement["flow_metadata"]["flow"] == "implement"
    assert test["flow_metadata"]["flow"] == "test"
    selector = resolve._flow_selector(resolve.COW_ROOT)
    context = {"operation": "test", "trigger": "test_requested",
               "task_shape": "test", "current_state": "IMPLEMENTED"}
    assert not selector.evaluate(implement["flow_metadata"]["when"], context)[0]
    assert selector.evaluate(test["flow_metadata"]["when"], context)[0]


def test_tuid_completion_is_claim_sensitive():
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
        subject_ref="component:buddy", goal_contract=goal))
    session = runtime.start(bundle, telemetry_disabled=True,
                            session_nonce="visual-claim")
    assert session._criterion_evidence_kinds("static_visual") == {"rendered_observation"}
    assert session._criterion_evidence_kinds("interaction") == {"interaction_validation"}
    assert session._criterion_evidence_kinds("terminal_lifecycle") == {"pty_validation"}
    assert session._criterion_evidence_kinds("code_behavior") == {"tool_result"}
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
    assert len(session.convergence.states()) == 1


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
    assert session.termination_disposition == "BLOCKED"
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


def test_twenty_turn_delivery_scenario_reanchors_instead_of_looping():
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

    observed_reanchor = False
    for turn in range(24):
        context = session.before_model_call(tool_capable=True)
        assert goal.objective in context
        assert "fuse" not in session.lifecycle.active
        assert "ACTION STRATEGY" not in context
        result = session.before_action({
            "tool_binding_id": "test", "arguments": {"turn": turn}})
        if result == runtime.GateDecision.REQUIRE_GOAL_REANCHOR:
            observed_reanchor = True
            assert "GOAL REANCHOR REQUIRED" in runtime.context_for(session)
            break
        assert result == runtime.GateDecision.ALLOW
        host.record_tool_result("test", "success", invocation_id=f"test-{turn}")
        assert "fuse" not in session.lifecycle.active
        assert "ward" not in session.lifecycle.active
    assert observed_reanchor


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
    pending = session._pending_action_requests[prepared.action_request_id]
    assert pending["relation"] == "advances_primary_goal"
    assert pending["accounting_lane"] == "DIRECT"
    evidence = host.record_tool_result(
        "read", "success", action_request_id=prepared.action_request_id,
        actual_result={"artifact_changed": True})
    state = session.convergence.state("primary-objective")
    assert state.total_actions_since_goal_progress == 1
    assert state.support_actions_since_goal_progress == 0

    host.record_goal_progress(cause_id="primary-objective", evidence_ref=evidence,
                              criterion_id="C1")
    state = session.convergence.state("primary-objective")
    assert state.total_actions_since_goal_progress == 0
    assert state.support_actions_since_goal_progress == 0


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
    pending = session._pending_action_requests[prepared.action_request_id]
    assert pending["relation"] == "ambiguous"
    assert pending["accounting_lane"] == "UNCLASSIFIED"
    host.record_tool_result("read", "success", action_request_id=prepared.action_request_id,
                            actual_result={"artifact_changed": False})
    state = session.convergence.state("primary-objective")
    assert state.total_actions_since_goal_progress == 1
    assert state.support_actions_since_goal_progress == 0
    assert session.convergence.state("primary-objective").last_goal_progress_seq == 0

    claim = CompletionClaim.create(
        claim_id="green-tests-only", task_id=bundle.task_id,
        subject_ref=bundle.policy.subject_ref, boundary_type="completion",
        boundary_target="task", expected_validator="completion-boundary-v1",
        invocation_id="tests", goal_contract_hash=goal.contract_hash,
        criterion_ids=("C1",))
    assert session.record_completion_claim(
        bundle.policy.subject_ref, [], claim=claim) == runtime.GateDecision.REQUIRE_REINSPECTION
