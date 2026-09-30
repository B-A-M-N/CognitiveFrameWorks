from runtime_contract.convergence import SharedBranchLedger


def test_inherited_premise_uses_shared_workflow_budget_and_new_premise_is_isolated(tmp_path):
    ledger = SharedBranchLedger(tmp_path / "shared-branches.sqlite3")
    scope = {
        "namespace_id": "tenant",
        "application_instance_id": "application-instance",
        "workflow_id": "workflow-1",
        "cause_id": "visual-layout",
    }

    assert ledger.record_failure(
        **scope, premise_digest="premise-shared", action_plan_fingerprint="plan-a",
        evidence_ref="evidence-a", attempt_ref="agent-a-attempt") == 1
    assert ledger.record_failure(
        **scope, premise_digest="premise-shared", action_plan_fingerprint="plan-b",
        evidence_ref="evidence-b", attempt_ref="agent-b-attempt") == 2
    assert ledger.recovery_count(**scope, premise_digest="premise-shared") == 2
    assert ledger.recovery_count(**scope, premise_digest="independent-premise") == 0

    # Replayed host evidence for the same attempt is idempotent.
    assert ledger.record_failure(
        **scope, premise_digest="premise-shared", action_plan_fingerprint="plan-a",
        evidence_ref="evidence-a", attempt_ref="agent-a-attempt") == 2
