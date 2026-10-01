"""Convergence controller, termination, lifecycle dispatch, and tri-state outcomes."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _modules():
    sys.path.insert(0, str(ROOT))
    from tests.test_combined_authorities import _runtime_modules
    return _runtime_modules()


IDENTITY = {"namespace_id": "ns", "application_instance_id": "inst",
            "agent_instance_id": "agent", "session_id": "sess",
            "task_id": "task", "attempt_id": "attempt-1", "interaction_id": None}


def _controller(**kwargs):
    from runtime_contract.convergence import ConvergenceController
    params = {"identity": IDENTITY}
    params.update(kwargs)
    return ConvergenceController(**params)


def test_reinspection_can_satisfy_the_retry_gate():
    controller = _controller()
    controller.record_failure(cause_id="c1", action_fingerprint="a1",
                             hypothesis_fingerprint="h1", evidence_ref="e1")
    assert controller.evaluate_retry(cause_id="c1", action_fingerprint="a1",
                                    hypothesis_fingerprint="h1") == "require_reinspection"
    controller.record_reinspection(cause_id="c1", revision=1)
    # Reinspection happened but nothing changed: a new hypothesis is required.
    assert controller.evaluate_retry(cause_id="c1", action_fingerprint="a1",
                                    hypothesis_fingerprint="h1") == "require_changed_hypothesis"


def test_changed_hypothesis_with_fresh_evidence_is_allowed():
    controller = _controller()
    controller.record_failure(cause_id="c1", action_fingerprint="a1",
                             hypothesis_fingerprint="h1", evidence_ref="e1")
    controller.record_reinspection(cause_id="c1", revision=1)
    assert controller.evaluate_retry(cause_id="c1", action_fingerprint="a2",
                                    hypothesis_fingerprint="h2") is None


def test_renamed_equivalent_approach_does_not_reset_the_budget():
    controller = _controller()
    for _ in range(2):
        controller.record_failure(cause_id="c1", action_fingerprint="a1",
                                 hypothesis_fingerprint="h1", evidence_ref="e1")
    controller.record_reinspection(cause_id="c1", revision=1)
    # Cosmetic rename: same fingerprint pair, so the budget still applies.
    assert controller.evaluate_retry(cause_id="c1", action_fingerprint="a1",
                                    hypothesis_fingerprint="h1") == "require_changed_hypothesis"


def test_objective_progress_resets_no_progress_budget():
    controller = _controller(max_recovery_epochs_without_progress=2)
    controller.record_failure(cause_id="c1", action_fingerprint="a1",
                             hypothesis_fingerprint="h1", evidence_ref="e1")
    controller.record_recovery_epoch(cause_id="c1")
    assert controller.state("c1").no_progress_epochs == 1
    # Genuine host-observed progress resets the no-progress budget.
    controller.record_progress(cause_id="c1", progress_digest="p1",
                               progress_class="GOAL_PROGRESS")
    assert controller.state("c1").no_progress_epochs == 0
    assert controller.state("c1").recovery_epochs == 0


def test_exhausted_budget_terminates_instead_of_reinspecting():
    controller = _controller(max_recovery_epochs_without_progress=1)
    controller.record_failure(cause_id="c1", action_fingerprint="a1",
                             hypothesis_fingerprint="h1", evidence_ref="e1")
    controller.record_recovery_epoch(cause_id="c1")
    assert controller.evaluate_retry(cause_id="c1", action_fingerprint="a2",
                                    hypothesis_fingerprint="h2") == "block"
    assert controller.state("c1").disposition == "BLOCKED"


def test_convergence_is_scoped_per_agent_and_cause():
    a = _controller()
    b = _controller()
    a.record_failure(cause_id="c1", action_fingerprint="x", hypothesis_fingerprint="y")
    b.record_failure(cause_id="c1", action_fingerprint="x", hypothesis_fingerprint="y")
    assert a.state("c1").identity.agent_instance_id == b.state("c1").identity.agent_instance_id
    assert a.state("c1").identity.key() != a.state("c2").identity.key()


def test_resume_cannot_erase_the_budget(tmp_path, monkeypatch):
    resolve, runtime = _modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    bundle = resolve.resolve("convergence-resume", "quick")
    events = []
    first = runtime.start(bundle, session_nonce="conv-resume",
                          telemetry_sink=lambda event: events.append(dict(event)))
    controller = first.convergence
    controller.record_failure(cause_id="c1", action_fingerprint="a1",
                             hypothesis_fingerprint="h1", evidence_ref="e1")
    first._record_convergence("test")
    resumed = runtime.resume_task(bundle, "conv-resume",
                                  telemetry_sink=lambda event: events.append(dict(event)))
    assert resumed.convergence.state("c1").same_approach_retries == 1


def test_blocked_task_terminates_without_claiming_success(tmp_path, monkeypatch):
    resolve, runtime = _modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    bundle = resolve.resolve("convergence-block", "quick")
    session = runtime.start(bundle, session_nonce="conv-block", telemetry_disabled=True)
    host = session._host_ingress()
    permit = host.request_termination(disposition="BLOCKED", reason="unresolvable premise",
                                      cause_id="c1")
    assert permit.disposition == "BLOCKED"
    assert session.before_retry({"tool_binding_id": "read", "arguments": {},
                                 "subject_ref": ""}) == runtime.GateDecision.BLOCK
    out = host.finish_task_terminated()
    assert out["status"] == "blocked" and out["completed"] is False


def test_anchor_open_task_start_predicate_activates(tmp_path, monkeypatch):
    resolve, runtime = _modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    bundle = resolve.resolve("lifecycle-anchor", "implement")
    session = runtime.start(bundle, session_nonce="anchor-task", telemetry_disabled=True)
    assert "anchor_open" in session.lifecycle.active


def test_segment_reset_does_not_reexecute_task_activation(tmp_path, monkeypatch):
    resolve, runtime = _modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    bundle = resolve.resolve("lifecycle-segment", "implement")
    session = runtime.start(bundle, session_nonce="segment-task", telemetry_disabled=True)
    before = dict(session.lifecycle.counts)
    session.lifecycle.reset_for_segment()
    assert session.lifecycle.counts["anchor_open"] == before["anchor_open"]
    assert "anchor_open" in session.lifecycle.active


def test_route_outcome_is_tri_state():
    """Absence of success evidence is not failure evidence."""
    import inspect
    from runtime import RuntimeSession
    source = inspect.getsource(RuntimeSession._evaluate_route_outcome)
    assert 'outcome = "NOT_APPLICABLE"' in source


def test_convergence_state_block_is_model_visible(tmp_path, monkeypatch):
    resolve, runtime = _modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    bundle = resolve.resolve("convergence-context", "quick")
    session = runtime.start(bundle, session_nonce="conv-context", telemetry_disabled=True)
    session.convergence.reject_path(cause_id="c17", action_fingerprint="a91",
                                   hypothesis_fingerprint="h", evidence_ref="v22",
                                   premise_digest=None, observed_at="now")
    context = runtime.context_for(session)
    assert "FAILED PATHS" in context and "c17" in context


def test_route_outcome_starts_as_not_applicable_not_failure():
    """The evaluator's default is absence of evidence, not a failure verdict.

    ``bad`` is still correct for explicit negative evidence; what must not
    happen is reaching it without any negative verdict at all.
    """
    import inspect
    from runtime import RuntimeSession
    source = inspect.getsource(RuntimeSession._evaluate_route_outcome)
    default = source.index('outcome = "NOT_APPLICABLE"')
    positive = source.index('outcome = "PASS"')
    negative = source.index('outcome = "FAIL"')
    assert default < positive < negative
    # Failure is only reachable behind an explicit failing status set.
    assert "in {\"fail\", \"failed\", \"failure\", \"error\"}" in source


def test_delegated_repeat_is_not_an_independent_recovery():
    """Agent fan-out must not bypass retry limits on an inherited premise."""
    controller = _controller()
    controller.record_failure(cause_id="c17", action_fingerprint="a91",
                             hypothesis_fingerprint="h", evidence_ref="e1",
                             premise_digest="premise-1")
    controller.record_reinspection(cause_id="c17", revision=1)
    result = controller.evaluate_retry(cause_id="c17", action_fingerprint="a91",
                                      hypothesis_fingerprint="h",
                                      premise_digest="premise-1", delegated=True)
    assert result == "require_changed_hypothesis"
    # The delegated repeat counts against the same branch budget.
    assert controller.state("c17").same_approach_retries == 2


def test_same_cause_and_premise_share_a_branch_key():
    controller = _controller()
    inherited = controller.branch_key(cause_id="c17", hypothesis_fingerprint="h",
                                      premise_digest="premise-1")
    same = controller.branch_key(cause_id="c17", hypothesis_fingerprint="h",
                                 premise_digest="premise-1")
    independent = controller.branch_key(cause_id="c17", hypothesis_fingerprint="h2",
                                        premise_digest="premise-2")
    assert inherited == same != independent


def test_rejected_paths_are_evidence_only_not_narrative():
    """Cross-agent transfer must convey the result, not another agent's reasoning."""
    controller = _controller()
    controller.reject_path(cause_id="c17", action_fingerprint="a91",
                           hypothesis_fingerprint="h", evidence_ref="v22",
                           premise_digest=None, observed_at="t0")
    block = controller.state("c17").compact_state_block(
        max_recovery_epochs_without_progress=3)
    assert "evidence only" in block.lower()
    assert "v22" in block


def test_route_outcome_records_origin_and_intended_target(tmp_path, monkeypatch):
    """Ambiguous delivery ("task-123 failed") must not be representable."""
    from tests.test_combined_authorities import _runtime_modules
    resolve, runtime = _runtime_modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    request = resolve.TaskRequest(
        task_id="origin-target", application_id="app", shape="implement",
        namespace_id="ns", application_instance_id="inst", premise_digest="premise-1")
    session = runtime.start(resolve.resolve(request), session_nonce="origin-target",
                            telemetry_disabled=True)
    host = session._host_ingress()
    decision_id = host.record_route_decision(route="owl", route_type="stage",
                                             selection_reason="host_selected")
    invocation = "inv-ot"
    host.record_tool_outcome("fixture", "pass", "s", invocation_id=invocation,
                             actual_result={"fresh": True, "status": "pass"})
    ref = host.record_validator_result("reinspection-v1", invocation,
                                       {"fresh": True, "status": "pass"}, "s")
    host.evaluate_route_outcome(route_decision_id=decision_id,
                                evaluator_id="route-success-v1", evidence_refs=[ref])
    outcome = next(iter(session.route_outcomes.values()))
    assert outcome["origin"]["agent_instance_id"] == session.agent_instance_id
    assert outcome["origin"]["premise_digest"] == "premise-1"
    assert outcome["target"]["scope"] in {"agent_private", "shared_workflow"}
    assert outcome["target"]["agent_instance_id"] == session.agent_instance_id
