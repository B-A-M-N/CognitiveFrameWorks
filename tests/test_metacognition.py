from __future__ import annotations

from runtime_contract.task_evidence import CompletionReadiness, TaskEvidenceState
from runtime_session.standalone_behavior_policy import (
    StandaloneBehaviorFacts, StandaloneBehaviorPolicy,
    render_standalone_behavior_guidance,
)
from runtime_contract.behavior_facts import WorkflowProgressFact
from runtime_contract.standalone_attention import StandaloneAttentionState
from runtime_contract.protocol_artifact import PROTOCOL_SCHEMA


def test_global_retry_and_recovery_counters_are_not_owned_by_cfw_metacognition():
    state = TaskEvidenceState()
    snapshot = state.snapshot()
    assert not {
        "actions_since_goal_progress", "goal_directed_actions_since_goal_progress",
        "recovery_epochs", "repeated_branch_count",
    } & snapshot.keys()

    # Older journals remain readable, but their global counters are discarded.
    legacy = dict(snapshot)
    legacy.update({
        "actions_since_goal_progress": 12,
        "goal_directed_actions_since_goal_progress": 3,
        "recovery_epochs": 2,
        "repeated_branch_count": 4,
    })
    restored = TaskEvidenceState()
    restored.restore(legacy)
    assert not {
        "actions_since_goal_progress", "goal_directed_actions_since_goal_progress",
        "recovery_epochs", "repeated_branch_count",
    } & restored.snapshot().keys()


def test_host_state_transition_advances_revision_without_adding_global_state():
    state = TaskEvidenceState()
    revision = state.logical_revision

    state.mark_host_state_transition()

    assert state.logical_revision == revision + 1
    assert not {"recovery_epochs", "repeated_branch_count"} & state.snapshot().keys()
    assert "criterion_attention" not in state.snapshot()
    assert not hasattr(state, "active_attention_window")


def test_observed_artifact_change_resets_local_verification_budget():
    state = TaskEvidenceState()
    verification = state.make_candidate(
        "action", action_class="test", target_ref="retry-validator",
        affected_criterion_ids=("retry_schedule",))
    for _ in range(2):
        state.commit_candidate(
            verification, verification_relation="required_verification")

    assert state.verification_limit_exceeded(
        verification, relation="required_verification",
        max_actions=2, max_repeats=1) == "verification_budget_exhausted"

    state.record_activity(
        "action", action_class="write", artifact_changed=True,
        affected_criterion_ids=("retry_schedule",))

    assert state.verification_limit_exceeded(
        verification, relation="required_verification",
        max_actions=2, max_repeats=1) is None


def test_progress_redirects_are_bounded_and_yield_attention_until_new_evidence():
    state = TaskEvidenceState()
    state.ensure_criteria({"acceptance_criteria": [
        {"criterion_id": item, "claim": "unfinished", "kind": "code_behavior"}
        for item in ("C1", "C2", "C3", "C4")
    ]})
    attention = StandaloneAttentionState()
    attention.sync_criteria(state.criteria)

    assert attention.record_progress_redirect(
        action_digest="action-a", criterion_id="C1", criteria=state.criteria) == "redirect"
    assert attention.record_progress_redirect(
        action_digest="action-a", criterion_id="C1", criteria=state.criteria) == "repeat"
    assert attention.take_progress_redirect_guidance() == "C1"
    assert attention.take_progress_redirect_guidance() is None

    assert attention.record_progress_redirect(
        action_digest="action-b", criterion_id="C1", criteria=state.criteria) == "yield"
    assert attention.active_attention_window(state.criteria) == ("C2", "C3", "C4")

    restored = StandaloneAttentionState()
    restored.restore(attention.snapshot(), criteria=state.criteria)
    assert restored.progress_redirected is True
    assert restored.progress_redirect_guidance_pending is True
    assert restored.consume_progress_redirect_yield("C2") is True
    assert restored.consume_progress_redirect_yield("C2") is False
    assert restored.take_progress_redirect_guidance() == "C1"
    assert restored.record_progress_redirect(
        action_digest="action-c", criterion_id="C1", criteria=state.criteria) == "limit"

    prior_epoch = restored.progress_redirect_epoch
    fact = state.record_activity("action", artifact_changed=True)
    restored.record_activity(fact)
    assert restored.progress_redirect_epoch == prior_epoch + 1
    assert restored.progress_redirected is False
    assert restored.record_progress_redirect(
        action_digest="action-a", criterion_id="C1", criteria=state.criteria) == "redirect"


def test_standalone_behavior_policy_does_not_repeat_redirect_after_yield():
    policy = StandaloneBehaviorPolicy()
    state = TaskEvidenceState()
    state.ensure_criteria({"acceptance_criteria": [
        {"criterion_id": "C1", "claim": "unfinished", "kind": "code_behavior"},
        {"criterion_id": "C2", "claim": "unfinished", "kind": "code_behavior"},
    ]})
    attention = StandaloneAttentionState()
    attention.sync_criteria(state.criteria)
    attention.record_progress_redirect(
        action_digest="action-a", criterion_id="C1", criteria=state.criteria)
    attention.take_progress_redirect_guidance()
    attention.record_progress_redirect(
        action_digest="action-b", criterion_id="C1", criteria=state.criteria)
    attention.take_progress_redirect_guidance()

    decision = policy.decide(
        StandaloneBehaviorFacts(
            active_criterion_ids=attention.active_attention_window(state.criteria),
            progress_redirect_guidance_pending=attention.progress_redirect_guidance_pending,
            progress_redirected=attention.progress_redirected),
        support_actions_since_progress=5, support_action_limit=5,
        total_actions_since_progress=15, total_action_limit=15,
        recovery_epochs_since_progress=0, max_recovery_epochs=3,
        same_approach_retries=0, max_same_approach_retries=2)

    assert decision.mode == "DELIVER"
    assert decision.active_criterion_ids == ("C2", "C1")


def test_total_action_budget_requires_explicit_goal_reanchor_guidance():
    policy = StandaloneBehaviorPolicy()
    decision = policy.decide(
        StandaloneBehaviorFacts(active_criterion_ids=("C1", "C2")),
        support_actions_since_progress=0, support_action_limit=5,
        total_actions_since_progress=15, total_action_limit=15,
        recovery_epochs_since_progress=0, max_recovery_epochs=3,
        same_approach_retries=0, max_same_approach_retries=2)

    assert decision.mode == "REPLAN"
    assert decision.goal_reanchor_required is True
    assert "Re-anchor the next action" in render_standalone_behavior_guidance(decision)


def test_completion_readiness_requires_boundary_evidence_and_complete_workflows():
    policy = StandaloneBehaviorPolicy()
    state = TaskEvidenceState()
    state.ensure_criteria({"acceptance_criteria": [
        {"criterion_id": "C1", "claim": "done", "kind": "code_behavior"}]})
    state.mark_criterion_satisfied("C1", evidence_ref="criterion-proof",
                                   evidence_kind="validator_result")

    decision = policy.decide(
        StandaloneBehaviorFacts(
            active_criterion_ids=StandaloneAttentionState().active_attention_window(
                state.criteria)),
        support_actions_since_progress=0, support_action_limit=5,
        total_actions_since_progress=0, total_action_limit=15,
        recovery_epochs_since_progress=0, max_recovery_epochs=3,
        same_approach_retries=0, max_same_approach_retries=2,
        completion_readiness=CompletionReadiness(
            required_criteria_satisfied=True, boundary_evidence_available=False,
            unresolved_contradictions=False, active_stateworks_complete=True,
            pending_verification_obligations=0))
    assert decision.mode == "VERIFY"

    ready = policy.decide(
        StandaloneBehaviorFacts(
            active_criterion_ids=StandaloneAttentionState().active_attention_window(
                state.criteria)),
        support_actions_since_progress=0, support_action_limit=5,
        total_actions_since_progress=0, total_action_limit=15,
        recovery_epochs_since_progress=0, max_recovery_epochs=3,
        same_approach_retries=0, max_same_approach_retries=2,
        completion_readiness=CompletionReadiness(
            required_criteria_satisfied=True, boundary_evidence_available=True,
            unresolved_contradictions=False, active_stateworks_complete=True,
            pending_verification_obligations=0))
    assert ready.mode == "READY_TO_COMPLETE"


def test_workflow_progress_projection_has_no_state_machine_only_fields():
    fact = WorkflowProgressFact(
        statework_id="release", subject_ref="release-42", engine_kind="dag",
        workflow_revision=3, step_id="package", work_class="build",
        domain_effect="artifact_created", progress_class="FORWARD_PROGRESS",
        current_position="package,sign", previous_position="package",
        completed=False, evidence_refs=("sha256:abc",))

    assert fact.to_dict() == {
        "statework_id": "release", "subject_ref": "release-42",
        "engine_kind": "dag", "workflow_revision": 3, "step_id": "package",
        "work_class": "build", "domain_effect": "artifact_created",
        "progress_class": "FORWARD_PROGRESS", "current_position": "package,sign",
        "previous_position": "package", "completed": False,
        "evidence_refs": ["sha256:abc"], "source": "csw_authoritative",
    }
    import jsonschema
    jsonschema.validate(fact.to_dict(), PROTOCOL_SCHEMA["$defs"]["WorkflowProgressFact"])


def test_old_workflow_snapshot_does_not_infer_completion():
    old = TaskEvidenceState().snapshot()
    old["workflows"] = [{
        "statework_id": "release", "domain": None, "purpose": None,
        "current_state": None, "previous_state": None, "edge_id": None,
        "trigger": None, "path_class": None, "progress_class": "TERMINAL",
        "work_class": "workflow", "domain_effect": "workflow_state",
        "revision": 2, "normal_path_rank_before": None,
        "normal_path_rank_after": None, "source": "csw_authoritative",
        "subject_ref": "release-42", "engine_kind": "dag",
        "step_id": "final-step", "completed": None,
    }]

    restored = TaskEvidenceState()
    restored.restore(old)

    assert restored.workflows[0].completed is False


def test_restore_preserves_multiple_subjects_for_one_statework():
    state = TaskEvidenceState()
    for subject_ref, step_id in (("repo-a", "inspect-a"), ("repo-b", "inspect-b")):
        state.record_workflow_progress(
            statework_id="gitter", subject_ref=subject_ref,
            engine_kind="state_machine", workflow_revision=1, step_id=step_id,
            progress_class="FORWARD_PROGRESS", completed=False,
            work_class="inspect", domain_effect="repository_observed",
            current_position="OBSERVED")

    restored = TaskEvidenceState()
    restored.restore(state.snapshot())

    assert {(item.statework_id, item.subject_ref) for item in restored.workflows} == {
        ("gitter", "repo-a"), ("gitter", "repo-b")}
