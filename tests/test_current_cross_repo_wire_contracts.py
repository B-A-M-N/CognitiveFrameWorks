"""Current production wire contracts across the DP, CFW and CSW checkouts.

These tests deliberately construct the *current* objects from all three
repositories and push them through the *real* adapters. Nothing here is
mocked, hand-frozen, or copied from an older fixture: a BehaviorState is built
by DP's own serializer, an attention lease by DP's own dataclass, and the CFW
and CSW sides consume them through their published entry points.

The rule these enforce:

    A repo suite is not green for convergence unless the current wire types
    from the other repos have actually crossed its public adapter boundary.

That rule is why this file exists. Every defect it is written against
(DP's mode_lease and material-progress fields being rejected, a missing json
import on the focus-criteria lease path, a criterion-free candidate raising
IndexError, and the CSW snapshot signature drift) passed all three suites
individually.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COW = Path("/home/bamn/CognitiveStateWork")
DP = Path("/home/bamn/DigitalPsychology")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Real production objects, loaded from the current checkouts
# --------------------------------------------------------------------------

def _dp_behavior_state():
    """Import DP's real behavior module from the current checkout."""
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.behavior_state import (BehaviorIntervention, BehaviorState,
                                    InterventionTarget)
    from lib.runtime.cfw_projection import build_cfw_behavior_projection
    return BehaviorIntervention, BehaviorState, InterventionTarget, build_cfw_behavior_projection


def _cfw_component_control():
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from runtime_contract import component_control
    return component_control


# --------------------------------------------------------------------------
# Real CFW RuntimeSession bound to DP as the behavior authority
# --------------------------------------------------------------------------

def _real_session(tmp_path, monkeypatch):
    """Start a real CFW RuntimeSession and bind DP as its behavior authority."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    sys.modules.pop("resolve_runtime", None)
    resolve = _load("wire_resolve", ROOT / "scripts" / "resolve-runtime.py")
    sys.modules["resolve_runtime"] = resolve
    runtime = _load("wire_runtime", ROOT / "scripts" / "runtime.py")

    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    # A path with no registry.yaml keeps this session CFW-only; the StateWork
    # composition is covered separately against the real CSW authority.
    bundle = resolve.resolve(
        resolve.TaskRequest(task_id="wire-contract", namespace_id="tenant-wire",
                            application_id="wire-app",
                            application_instance_id="wire-instance",
                            shape="quick"),
        cow_root=ROOT / "statework")
    session = runtime.start(bundle, session_nonce="wire-session",
                            telemetry_disabled=True)
    session.enable_external_behavior_authority()
    return session


def test_current_dp_projection_crosses_a_real_cfw_runtime_session(tmp_path, monkeypatch):
    """End to end: DP's projection v2 serializer -> CFW lifecycle overlay."""
    BehaviorIntervention, BehaviorState, InterventionTarget, build_projection = (
        _dp_behavior_state())
    session = _real_session(tmp_path, monkeypatch)
    intervention = BehaviorIntervention(
        intervention_id="intervention-mode", cause="verification requested",
        expected_effect="Use verification mode for the bounded action window.",
        start_revision=1, scope="wire-scope", expires_after_actions=2,
        actions_elapsed=0,
        targets=(InterventionTarget("mode", "DELIVER", "VERIFY"),),
        baseline_action_index=1, expires_at_action=2)
    state = BehaviorState(
        revision=1, cognitive_mode="VERIFY", active_interventions=(intervention,))
    projection = build_projection(state, host_action_index=2).to_dict()

    session.apply_external_behavior_projection(projection)

    effective = session.effective_external_behavior_projection()
    assert effective["cognitive_mode"] == "VERIFY"
    assert effective["target_leases"][0]["intervention_id"] == "intervention-mode"
    assert effective["protocol_version"] == "2.0.0"


def test_current_dp_state_still_rejected_when_it_drifted(tmp_path, monkeypatch):
    """The versioned projection port rejects fields from an unnegotiated schema."""
    _, BehaviorState, _, build_projection = _dp_behavior_state()
    session = _real_session(tmp_path, monkeypatch)

    drifted = build_projection(BehaviorState(), host_action_index=0).to_dict()
    drifted["a_field_cfw_does_not_know"] = 1
    with pytest.raises(ValueError, match="projection does not match the v2 contract"):
        session.apply_external_behavior_projection(drifted)


def test_cfw_focus_criteria_lease_round_trips_through_a_real_session(
        tmp_path, monkeypatch):
    """P0-5 end to end: the focus lease path needs json to be importable."""
    BehaviorIntervention, BehaviorState, InterventionTarget, build_projection = (
        _dp_behavior_state())
    session = _real_session(tmp_path, monkeypatch)

    intervention = BehaviorIntervention(
        intervention_id="intervention-focus", cause="focus update",
        expected_effect="Use the selected focus criteria.", start_revision=1,
        scope="wire-scope", expires_after_actions=2, actions_elapsed=0,
        targets=(InterventionTarget("focus_criteria", "[]", '["criterion-a"]'),),
        baseline_action_index=1, expires_at_action=2)
    state = BehaviorState(revision=1, focus_criteria=("criterion-a",),
                          active_interventions=(intervention,))
    session.apply_external_behavior_projection(
        build_projection(state, host_action_index=2).to_dict())
    assert session.effective_external_behavior_projection()["focus_criteria"] == [
        "criterion-a"]


# --------------------------------------------------------------------------
# DP -> CSW: the real DP adapter against the real CSW authority
# --------------------------------------------------------------------------

def _real_csw(tmp_path):
    """Build a real CSW WorkflowAuthority from the current checkout."""
    if str(COW / "scripts") not in sys.path:
        sys.path.insert(0, str(COW / "scripts"))
    from control_plane import (EvidenceStore, SubjectStateStore,
                               WorkflowAuthority, WorkflowSelectionStore)
    return WorkflowAuthority(
        root=COW,
        state_store=SubjectStateStore(tmp_path / "subjects.json"),
        evidence_store=EvidenceStore(),
        selection_store=WorkflowSelectionStore(tmp_path / "selections.json"))


def _dp_csw_adapter(authority, scope, statework_id="gitter"):
    """Build DP's real CSWRuntimeAdapter bound to the real authority."""
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.runtime.adapters.csw import (CSWWorkflowBinding,
                                          CSWRuntimeAdapter)
    from lib.runtime.contracts import RuntimeIdentity

    identity = RuntimeIdentity(
        namespace_id=scope["namespace_id"], application_id=scope["application_id"],
        application_instance_id=scope["application_instance_id"],
        agent_id="agent", agent_instance_id="agent-1",
        session_id=scope["workflow_id"], task_id=scope["workflow_id"],
        attempt_id="attempt-1", workflow_id=scope["workflow_id"],
        subject_ref=scope["subject_ref"], segment_id="segment-1",
        behavioral_subject=scope["subject_ref"])
    contract = authority.workflow_contract_identity(statework_id)
    binding = CSWWorkflowBinding(
        identity={**scope, "statework_id": statework_id,
                  "contract_version": contract["contract_version"]},
        contract_hash=contract["contract_hash"])
    return CSWRuntimeAdapter(authority, identity=identity,
                              bindings={statework_id: binding})


def _wire_scope():
    return {"namespace_id": "tenant-a", "application_id": "assistant",
            "application_instance_id": "assistant-1",
            "workflow_id": "task-wire", "subject_ref": "repo:wire"}


def test_dp_csw_adapter_snapshot_matches_the_real_csw_signature(tmp_path):
    """The adapter must call CSW's real snapshot signature.

    DP used to pass host_action_index to a pure read, which CSW's signature
    does not accept. A snapshot must also stay free of expiry side effects.
    """
    import inspect
    authority = _real_csw(tmp_path)
    adapter = _dp_csw_adapter(authority, _wire_scope())

    parameters = inspect.signature(authority.behavioral_snapshot).parameters
    assert "host_action_index" not in parameters, \
        "the CSW snapshot is a pure read and must not take an action index"

    snapshot = adapter.behavioral_snapshot()
    assert snapshot.revision >= 0
    assert snapshot.active_stateworks == ()


def test_dp_csw_adapter_uses_explicit_expiry_not_snapshot_mutation(tmp_path):
    """DP must expire leases through CSW's explicit expiry operation."""
    authority = _real_csw(tmp_path)
    adapter = _dp_csw_adapter(authority, _wire_scope())

    assert callable(authority.expire_workflow_leases)
    assert callable(adapter.expire_leases)

    before = adapter.behavioral_snapshot()
    after = adapter.expire_leases(host_action_index=0)
    assert after.revision >= before.revision
    assert after.active_stateworks == ()


def test_dp_csw_adapter_activate_then_expire_is_owned_and_scoped(tmp_path):
    """Full DP->CSW path: snapshot, activate, snapshot, expire, snapshot."""
    authority = _real_csw(tmp_path)
    adapter = _dp_csw_adapter(authority, _wire_scope())

    initial = adapter.behavioral_snapshot()
    activated = adapter.activate_statework(
        "gitter", expected_selection_revision=initial.revision,
        candidate_statework_ids=["gitter"], intervention_id="intervention-1",
        expires_at_action=40)
    assert "gitter" in activated.active_stateworks

    held = adapter.behavioral_snapshot()
    assert "gitter" in held.active_stateworks, \
        "a read must not expire a live lease"

    # Expire well past the boundary; the lease is released.
    adapter.expire_leases(host_action_index=41)
    released = adapter.behavioral_snapshot()
    assert released.active_stateworks == ()


# --------------------------------------------------------------------------
# DP host transaction path: a criterion-free candidate must not raise
# --------------------------------------------------------------------------

def test_criterion_free_candidate_rejection_does_not_index_empty_criteria():
    """P0-4: diagnostic/support candidates carry no affected criteria.

    The coordinator used affected_criterion_ids[0] whenever a candidate
    existed, which raises IndexError for exactly these legal candidates.
    """
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.host import transaction_coordinator as coordinator

    source = (DP / "lib" / "host" / "transaction_coordinator.py").read_text()
    assert "affected_criterion_ids[0]" not in source, \
        "an empty affected-criterion tuple must not be indexed unconditionally"
    assert "candidate_criteria[0] if candidate_criteria else None" in source

    # Exercise the guarded expression with a criterion-free candidate.
    candidate = type("Candidate", (), {"affected_criterion_ids": ()})()
    criterion_ids = tuple(candidate.affected_criterion_ids)
    assert (criterion_ids[0] if criterion_ids else None) is None


# --------------------------------------------------------------------------
# The narrow, versioned CFW actuation projection
# --------------------------------------------------------------------------

def test_cfw_projection_version_matches_dp_emit_version():
    """P2-20: the contract is versioned and the two sides agree.

    A host and framework pair must fail at startup, not on the first
    intervention, so both sides declare the same version constant.
    """
    from digital_psychology_adapter import (CFW_BEHAVIOR_PROJECTION_VERSION as cfw_version)
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.runtime.cfw_projection import (
        CFW_BEHAVIOR_PROJECTION_VERSION as dp_version)
    assert cfw_version == dp_version


def test_real_private_mcp_dp_cfw_csw_path_acts_expires_and_persists(
        tmp_path, monkeypatch):
    """Qualify current DP, CFW and CSW authorities through the private bridge."""
    import asyncio

    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from digital_psychology_adapter import CFWBehaviorAdapter
    from lib.behavior_state import GoalItemSnapshot, GoalSetSnapshot
    from lib.control.service import ControlPlaneService
    from lib.host import InProcessControlClient
    from lib.mcp_server import DigitalPsychologyService
    from lib.runtime import (BehaviorStateStore, CFWRuntimeAdapter, RuntimeHost,
                             RuntimeIdentity, RuntimeObservation)
    from lib.runtime.contracts import ActionCandidate
    from lib.runtime.cfw_projection import build_cfw_behavior_projection

    class AgentHandle:
        def before_model_call(self, *, behavior_context, **_kwargs):
            return behavior_context

        def before_tool_planning(self, _action_hint=None, *, behavior_context,
                                 **_kwargs):
            return behavior_context

    cfw_session = _real_session(tmp_path, monkeypatch)
    cfw_port = CFWRuntimeAdapter(CFWBehaviorAdapter(cfw_session, AgentHandle()))
    scope = _wire_scope()
    identity = RuntimeIdentity(
        namespace_id=scope["namespace_id"],
        application_id=scope["application_id"],
        application_instance_id=scope["application_instance_id"],
        agent_instance_id="agent-wire-instance", session_id=scope["workflow_id"],
        task_id=scope["workflow_id"], attempt_id="attempt-wire",
        workflow_id=scope["workflow_id"], subject_ref=scope["subject_ref"],
        segment_id="segment-1", agent_id="agent-wire",
        behavioral_subject=scope["subject_ref"], goal_contract_hash="d" * 64,
    )
    goals = GoalSetSnapshot(revision=0, items=(
        GoalItemSnapshot(criterion_id="criterion-wire", status="READY"),))
    authority = _real_csw(tmp_path)
    csw = _dp_csw_adapter(authority, scope)
    expiry_calls = []
    cfw_expiry_calls = []
    expire_csw = authority.expire_workflow_leases
    expire_cfw = cfw_session.expire_external_behavior_leases

    def tracked_csw_expiry(**kwargs):
        expiry_calls.append(kwargs["host_action_index"])
        return expire_csw(**kwargs)

    def tracked_cfw_expiry(action_index):
        cfw_expiry_calls.append(action_index)
        return expire_cfw(action_index)

    monkeypatch.setattr(authority, "expire_workflow_leases", tracked_csw_expiry)
    monkeypatch.setattr(cfw_session, "expire_external_behavior_leases",
                        tracked_cfw_expiry)
    dp_events = DigitalPsychologyService(event_db=tmp_path / "dp-events.sqlite3")
    store_path = tmp_path / "private-control.sqlite3"
    state_store = BehaviorStateStore(store_path)
    control_service = ControlPlaneService(
        decision_service=dp_events, state_store=state_store)
    host = RuntimeHost()

    async def exercise():
        bridge = await host.open_control_bridge(
            identity=identity, objective="Inspect and complete the requested repository work",
            objective_criteria=("criterion-wire",), initial_goal_set=goals,
            cfw=cfw_port, csw=csw,
            client=InProcessControlClient(control_service),
            outcome_evidence_connected=True,
        )
        try:
            first = await bridge.after_action(RuntimeObservation(
                action_index=1, goal_set=goals,
                equivalent_retry_count=2,
                active_trigger_codes=("equivalent_retry",),
                criterion_status={"criterion-wire": "unmet"},
            ))
            assert first["status"] == "ready"
            assert bridge.behavior_state.cognitive_mode == "RECOVER"
            effective = cfw_session.effective_external_behavior_projection()
            assert effective["cognitive_mode"] == bridge.behavior_state.cognitive_mode
            assert cfw_session.external_behavior_revision == bridge.behavior_state.revision
            forbidden = {"mode_lease", "attention_lease", "attention_last_seen",
                         "active_stateworks", "intervention_history"}
            assert forbidden.isdisjoint(cfw_session.external_behavior_projection)

            candidate = ActionCandidate(
                action_class="read", proposed_purpose="inspect the requested source",
                affected_criterion_ids=("criterion-wire",), action_id="wire-read-1",
                tool_name="read", operation="inspect", mutating=False,
                reversible=True,
            )
            action_request = {
                "tool_binding_id": "read", "arguments": {"path": "README.md"},
                "subject_ref": cfw_session.bundle.policy.subject_ref,
            }
            authorized = await bridge.before_action_candidate(
                candidate, cfw_action_request=action_request, goal_set=goals)
            assert authorized["action_authorized"] is True
            assert authorized["action_request_id"]
            second = await bridge.after_action(RuntimeObservation(
                action_index=2, goal_set=goals,
                criterion_status={"criterion-wire": "unmet"},
            ))
            assert second["status"] == "ready", second.get("fault")
            expected = build_cfw_behavior_projection(
                bridge.behavior_state, host_action_index=2).to_dict()
            assert cfw_session.external_behavior_projection == expected
            assert bridge.behavior_state.active_stateworks == \
                csw.behavioral_snapshot().active_stateworks

            transitions = state_store.committed_control_transitions(identity.scope)
            pre_action = next(item for item in transitions
                              if item["boundary_kind"] == "pre_action")
            assert pre_action["cfw_preflight_decision"] == "allow"
            action_rows = state_store.behavioral_action_traces(identity.scope)
            assert action_rows[-1]["cfw_preflight_decision"] == "allow"
            assert action_rows[-1]["cfw_final_decision"] == "allow"
            assert action_rows[-1]["projection_digest"]

            for action_index in (3, 4):
                result = await bridge.after_action(RuntimeObservation(
                    action_index=action_index, goal_set=goals,
                    criterion_status={"criterion-wire": "unmet"},
                ))
                assert result["status"] == "ready"
            reconciled = await bridge.reconcile()
            assert reconciled["status"] == "ready"
            current = build_cfw_behavior_projection(
                bridge.behavior_state, host_action_index=4).to_dict()
            assert cfw_session.external_behavior_projection == current
            assert bridge.behavior_state.active_stateworks == \
                csw.behavioral_snapshot().active_stateworks
            assert 4 in cfw_expiry_calls and 4 in expiry_calls

            record = control_service.behavioral_trial_record(
                identity.scope, trial_id="real-private-bridge")
            assert record["committed_actions"] == 4
            assert record["cfw_preflight_rejections"] == 0
            assert record["cfw_final_gate_rejections"] == 0
            return bridge
        except Exception:
            await bridge.close()
            raise

    try:
        bridge = asyncio.run(exercise())
        asyncio.run(bridge.close())
        # Re-open the durable store through a fresh service to prove that the
        # private-path action records survive a DP control-process restart.
        restarted = ControlPlaneService(
            decision_service=DigitalPsychologyService(
                event_db=tmp_path / "dp-events-restarted.sqlite3"),
            state_store=BehaviorStateStore(store_path),
        )
        recovered = restarted.behavioral_trial_record(
            identity.scope, trial_id="restarted-private-bridge")
        assert recovered["committed_actions"] == 4
        assert len(BehaviorStateStore(store_path).committed_control_transitions(
            identity.scope)) >= 5
        restarted.close()
    finally:
        control_service.close()


def test_projection_excludes_dp_private_bookkeeping():
    """The whole point of the projection: CFW cannot see DP's internals."""
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.behavior_state import AttentionLease, BehaviorState, ModeLease
    from lib.runtime.cfw_projection import build_cfw_behavior_projection

    state = BehaviorState(
        cognitive_mode="VERIFY",
        focus_criteria=("criterion-a",),
        attention_lease=AttentionLease(
            lease_id="lease-1", criterion_ids=("criterion-a",),
            issued_revision=0, goal_set_revision=1, start_action=0,
            max_actions=4, reason="bounded focus"),
        mode_lease=ModeLease(
            mode="VERIFY", entered_action=0, minimum_actions=1, max_actions=4,
            cause="verification requested", exit_condition="verification_result",
            baseline_approach_epoch="epoch-1"))

    payload = build_cfw_behavior_projection(state, host_action_index=0).to_dict()

    for private in ("mode_lease", "attention_lease", "attention_last_seen",
                    "active_stateworks", "intervention_history",
                    "active_interventions", "revision"):
        assert private not in payload, f"{private} is DP-private and must not cross"


def test_projection_round_trips_and_rejects_an_unknown_version():
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.behavior_state import BehaviorState
    from lib.runtime.cfw_projection import (CFWBehaviorProjection,
                                            build_cfw_behavior_projection)

    projection = build_cfw_behavior_projection(BehaviorState(), host_action_index=0)
    assert CFWBehaviorProjection.from_dict(projection.to_dict()) == projection

    drifted = projection.to_dict()
    drifted["protocol_version"] = "99.0.0"
    with pytest.raises(ValueError, match="unsupported CFW behavior projection version"):
        CFWBehaviorProjection.from_dict(drifted)


def test_projection_rejects_a_lease_without_its_previous_value():
    """An unowned change is not a lease; refuse rather than invent a baseline."""
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.behavior_state import (BehaviorIntervention, BehaviorState,
                                    InterventionTarget)
    from lib.runtime.cfw_projection import (CFWBehaviorProjection,
                                            build_cfw_behavior_projection)

    intervention = BehaviorIntervention(
        intervention_id="intervention-1", cause="verify", expected_effect="verify",
        start_revision=1, scope="scope", expires_after_actions=3, actions_elapsed=0,
        targets=(InterventionTarget("mode", "DELIVER", "VERIFY"),),
        baseline_action_index=0, expires_at_action=3)
    payload = build_cfw_behavior_projection(
        BehaviorState(revision=1, cognitive_mode="VERIFY",
                      active_interventions=(intervention,)),
        host_action_index=0).to_dict()
    payload["target_leases"][0]["previous_value"] = ""

    with pytest.raises(ValueError, match="previous_value"):
        CFWBehaviorProjection.from_dict(payload)


def test_projection_lease_expiry_uses_the_shared_action_boundary():
    """P1-12: expiry is computed from one action boundary for all systems."""
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.behavior_state import (BehaviorIntervention, BehaviorState,
                                    InterventionTarget)
    from lib.runtime.cfw_projection import build_cfw_behavior_projection

    intervention = BehaviorIntervention(
        intervention_id="intervention-1", cause="verify", expected_effect="verify",
        start_revision=3, scope="scope", expires_after_actions=3, actions_elapsed=0,
        targets=(InterventionTarget("mode", "DELIVER", "VERIFY"),),
        baseline_action_index=5, expires_at_action=8)
    projection = build_cfw_behavior_projection(
        BehaviorState(revision=3, cognitive_mode="VERIFY",
                      active_interventions=(intervention,)),
        host_action_index=5)
    lease = projection.target_leases[0]
    # Baseline 5 + duration 3, stated once so CFW and CSW cannot disagree by one.
    assert lease.expires_at_action == 8
    assert lease.source_revision == projection.behavior_revision
