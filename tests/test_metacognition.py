from __future__ import annotations

from runtime_contract.metacognition import MetacognitiveState


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
