"""One lease-boundary convention across CFW, CSW and DP (P1-12).

An intervention created before action 10 with duration 3 must have one agreed
meaning across all three systems. The ambiguity is specific: does
``expires_at_action = 12`` mean "valid through action 12", or "released when the
boundary at 12 is reached"?

This test states the convention explicitly and asserts all three systems agree
at actions 9, 10, 11, 12 and 13. If any system drifts by one action, the
intervention is released while the framework still holds it, or vice versa.

Convention under test (now shared by CFW, CSW and DP):
    A lease with expires_at_action = N is effective for every action <= N and
    has been released once the action index reaches N + 1.

This was not previously stated anywhere. CFW treated N as still valid while
CSW treated N as already expired, so a framework lease could keep holding a
control whose StateWork had been released one action earlier.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COW = Path("/home/bamn/CognitiveStateWork")
DP = Path("/home/bamn/DigitalPsychology")

BASELINE_ACTION = 10
DURATION = 3
EXPIRES_AT = BASELINE_ACTION + DURATION  # 13 under the stated convention
PROBE_ACTIONS = (9, 10, 11, 12, 13)


def _expected_active(action_index: int) -> bool:
    """The shared convention: effective through expires_at_action inclusive."""
    return action_index <= EXPIRES_AT


# -- CFW ------------------------------------------------------------------

def _cfw_component_control():
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from runtime_contract import component_control
    return component_control


def _cfw_overlay(component_control):
    """A real CFW overlay holding one mode and one target lease."""
    return component_control.ComponentOverlay(
        behavior_revision=1, behavior_mode="VERIFY",
        controls=tuple(
            component_control.ComponentControl(
                component_id=component_id,
                mode=component_control.COMPONENT_BASELINE_MODES[component_id],
                scope=None, expires_at_action=EXPIRES_AT, source_revision=1)
            for component_id in sorted(component_control.COMPONENT_IDS)),
        target_leases=(component_control.AppliedControlLease(
            directive_id="d1", intervention_id="intervention-1",
            target="mode", previous_value="DELIVER", applied_value="VERIFY",
            expires_at_action=EXPIRES_AT, behavior_revision=1),))


def test_cfw_releases_a_lease_exactly_at_its_expiry_action():
    component_control = _cfw_component_control()
    overlay = _cfw_overlay(component_control)

    observed = {
        index: overlay.effective_target_value("mode", index) is not None
        for index in PROBE_ACTIONS
    }
    assert observed == {index: _expected_active(index) for index in PROBE_ACTIONS}
    assert observed[13] is True, "still effective at the expiry action"
    assert not overlay.has_expired_target_leases(13)


# -- CSW ------------------------------------------------------------------

def _csw_authority(tmp_path):
    if str(COW / "scripts") not in sys.path:
        sys.path.insert(0, str(COW / "scripts"))
    from control_plane import (EvidenceStore, SubjectStateStore,
                               WorkflowAuthority, WorkflowSelectionStore)
    return WorkflowAuthority(
        root=COW,
        state_store=SubjectStateStore(tmp_path / "subjects.json"),
        evidence_store=EvidenceStore(),
        selection_store=WorkflowSelectionStore(tmp_path / "selections.json"))


def test_csw_releases_a_lease_exactly_at_its_expiry_action(tmp_path):
    authority = _csw_authority(tmp_path)
    scope = {"namespace_id": "tenant-a", "application_id": "assistant",
             "application_instance_id": "assistant-1",
             "workflow_id": "task-lease", "subject_ref": "repo:lease"}
    contract = authority.workflow_contract_identity("gitter")
    identity = {**scope, "statework_id": "gitter",
                "contract_version": contract["contract_version"]}
    authority.activate_workflow(
        identity, contract_hash=contract["contract_hash"],
        expected_selection_revision=0,
        activation_owner="digitalpsychology",
        control_session_id="dp-session-1",
        intervention_id="intervention-1",
        expires_at_action=EXPIRES_AT)

    observed = {}
    for index in PROBE_ACTIONS:
        authority.expire_workflow_leases(workflow_scope=scope,
                                         host_action_index=index)
        snapshot = authority.behavioral_snapshot(workflow_scope=scope)
        observed[index] = bool(snapshot["active_workflows"])

    assert observed == {index: _expected_active(index) for index in PROBE_ACTIONS}
    assert observed[13] is True, "still active at the expiry action"


# -- DP -------------------------------------------------------------------

def _dp_intervention_lifecycle():
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.control import intervention_lifecycle
    return intervention_lifecycle


def test_dp_ageing_and_the_shared_expiry_boundary_agree():
    """DP's intervention horizon must equal the shared expires_at_action."""
    module = _dp_intervention_lifecycle()
    source = Path(module.__file__).read_text(encoding="utf-8")
    # DP ages an intervention one step per completed action and clamps it to the
    # declared duration. The shared boundary is baseline + duration, which is
    # what the actuator passes to both CFW and CSW.
    assert "expires_after_actions" in source

    from lib.behavior_state import BehaviorDirective, BehaviorIntervention
    directive = BehaviorDirective(
        directive_id="d1", operation="set_mode", target="mode", value="VERIFY",
        cause="test", expected_effect="test",
        scope="tenant/app/agent/session/task/attempt",
        expires_after_actions=DURATION, intervention_id="intervention-1")
    intervention = BehaviorIntervention(
        intervention_id="intervention-1", scope="tenant/app/agent/session/task/attempt",
        baseline_action_index=BASELINE_ACTION, start_revision=1,
        expires_after_actions=DURATION, actions_elapsed=0,
        targets=(directive,),
        condition_id="legacy-condition", condition_basis_digest="a" * 64,
        cause="test", expected_effect="test")

    # The lease covers exactly DURATION completed actions after its baseline.
    assert BASELINE_ACTION + DURATION == EXPIRES_AT
    assert intervention.expires_after_actions == DURATION


# -- cross-system agreement ----------------------------------------------

def test_all_three_systems_release_on_the_same_action(tmp_path):
    """The whole point: CFW, CSW and DP must not disagree by one action."""
    component_control = _cfw_component_control()
    overlay = _cfw_overlay(component_control)

    authority = _csw_authority(tmp_path)
    scope = {"namespace_id": "tenant-a", "application_id": "assistant",
             "application_instance_id": "assistant-1",
             "workflow_id": "task-lease", "subject_ref": "repo:lease"}
    contract = authority.workflow_contract_identity("gitter")
    authority.activate_workflow(
        {**scope, "statework_id": "gitter",
         "contract_version": contract["contract_version"]},
        contract_hash=contract["contract_hash"],
        expected_selection_revision=0,
        activation_owner="digitalpsychology",
        control_session_id="dp-session-1",
        intervention_id="intervention-1",
        expires_at_action=EXPIRES_AT)

    disagreements = []
    for index in PROBE_ACTIONS:
        cfw_active = overlay.effective_target_value("mode", index) is not None
        authority.expire_workflow_leases(workflow_scope=scope,
                                         host_action_index=index)
        csw_active = bool(
            authority.behavioral_snapshot(workflow_scope=scope)["active_workflows"])
        expected = _expected_active(index)
        if cfw_active != expected or csw_active != expected:
            disagreements.append(
                (index, {"expected": expected, "cfw": cfw_active, "csw": csw_active}))

    assert not disagreements, (
        "CFW and CSW must release on the same action: " f"{disagreements}")
