from __future__ import annotations

from runtime_contract.metacognition import (CompletionReadiness,
                                             MetacognitiveController,
                                             MetacognitiveState)
from runtime_contract.system_one import WorkflowProgressFact
from runtime_contract.protocol_artifact import PROTOCOL_SCHEMA


def test_retry_accounting_only_changes_when_committed_and_survives_restore():
    state = MetacognitiveState()

    # A proposal, including one that the host later blocks, is not history.
    blocked = state.make_candidate("retry", result_class="equivalent_retry")
    assert state.repeated_branch_count == 0

    state.commit_candidate(state.make_candidate("retry", result_class="changed_hypothesis"))
    assert state.repeated_branch_count == 0

    state.commit_candidate(state.make_candidate("retry", result_class="equivalent_retry"))
    assert state.repeated_branch_count == 1

    restored = MetacognitiveState()
    restored.restore(state.snapshot())
    assert restored.repeated_branch_count == 1

    # Keep the blocked candidate deliberately uncommitted.
    assert blocked.result_class == "equivalent_retry"
    assert restored.repeated_branch_count == 1


def test_completion_readiness_requires_boundary_evidence_and_complete_workflows():
    controller = MetacognitiveController()
    state = MetacognitiveState()
    state.ensure_criteria({"acceptance_criteria": [
        {"criterion_id": "C1", "claim": "done", "kind": "code_behavior"}]})
    state.mark_criterion_satisfied("C1", evidence_ref="criterion-proof",
                                   evidence_kind="validator_result")

    decision = controller.evaluate(
        state, support_actions_since_progress=0, support_action_limit=5,
        total_actions_since_progress=0, total_action_limit=15,
        recovery_epochs_since_progress=0, max_recovery_epochs=3,
        same_approach_retries=0, max_same_approach_retries=2,
        completion_readiness=CompletionReadiness(
            required_criteria_satisfied=True, boundary_evidence_available=False,
            unresolved_contradictions=False, active_stateworks_complete=True,
            pending_verification_obligations=0))
    assert decision.mode == "VERIFY"

    ready = controller.evaluate(
        state, support_actions_since_progress=0, support_action_limit=5,
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
    old = MetacognitiveState().snapshot()
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

    restored = MetacognitiveState()
    restored.restore(old)

    assert restored.workflows[0].completed is False


def test_restore_preserves_multiple_subjects_for_one_statework():
    state = MetacognitiveState()
    for subject_ref, step_id in (("repo-a", "inspect-a"), ("repo-b", "inspect-b")):
        state.record_workflow_progress(
            statework_id="gitter", subject_ref=subject_ref,
            engine_kind="state_machine", workflow_revision=1, step_id=step_id,
            progress_class="FORWARD_PROGRESS", completed=False,
            work_class="inspect", domain_effect="repository_observed",
            current_position="OBSERVED")

    restored = MetacognitiveState()
    restored.restore(state.snapshot())

    assert {(item.statework_id, item.subject_ref) for item in restored.workflows} == {
        ("gitter", "repo-a"), ("gitter", "repo-b")}
