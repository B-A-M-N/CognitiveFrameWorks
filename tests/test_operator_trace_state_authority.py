from __future__ import annotations

from types import SimpleNamespace

from runtime_session.action_gate import ActionGateMethods


def _session(authority):
    session = object.__new__(ActionGateMethods)
    session.bundle = SimpleNamespace(
        policy=SimpleNamespace(subject_ref="repo:trace"),
        stateworks=({"id": "gitter", "workflow_contract_hash": "a" * 64,
                    "engine": {"kind": "state_machine"}},),
        guard_pack={}, task_id="operator-trace", pinned={"policy_hash": "b" * 64})
    session.lifecycle_workflow = SimpleNamespace(
        statework_states={("gitter", "repo:trace"): "CACHED"})
    session.identity_authority = SimpleNamespace(
        identity=SimpleNamespace(session_id="session", attempt_id="attempt",
                                 agent_instance_id="agent-instance"),
        authorities=SimpleNamespace(state_authority=authority))
    session.actions = SimpleNamespace(route_decisions={}, route_outcomes={})
    return session


def test_operator_trace_uses_live_state_authority_snapshot():
    authority = SimpleNamespace(inspect_workflow=lambda **_: SimpleNamespace(
        current_state="OBSERVED", revision=9, engine_kind="state_machine"))

    state = _session(authority)._operator_trace()["statework"][0]

    assert state == {"statework_id": "gitter", "subject_ref": "repo:trace",
                     "state": "OBSERVED", "engine_kind": "state_machine",
                     "revision": 9}


def test_operator_trace_reports_unavailable_authority_with_pinned_engine_kind():
    class UnavailableAuthority:
        def inspect_workflow(self, **_):
            raise RuntimeError("offline")

    state = _session(UnavailableAuthority())._operator_trace()["statework"][0]

    assert state["state"] == "unavailable:RuntimeError"
    assert state["engine_kind"] == "state_machine"
    assert state["revision"] is None
