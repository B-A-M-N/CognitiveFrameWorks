"""CFW slice B: control lifecycle expiry, action-gate learning signals, projection.

Lifecycle: applied controls expire at their declared action boundary
(effective through N, released at N+1 -- the shared CFW/CSW/DP convention),
while unrelated controls and WARD dependencies survive the expiry.
Restart rebuilds actuator state from persisted DP state; a concurrent
revision conflict fails closed and recovers through rollback.

Action gate: every block records a coarse reason class
(authority/policy/evidence/capability) and exports a bounded semantic
reason for DP learning that never carries private tool arguments.

Projection: the model sees consequences of active controls only -- no DP
admin internals, hidden mechanisms, or policy internals.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from runtime_contract.actions import ActionDescriptor
from runtime_contract.component_control import (
    COMPONENT_BASELINE_MODES,
    COMPONENT_IDS,
    AppliedControlLease,
    ComponentControl,
    ComponentOverlay,
)
from runtime_session.action_gate import (
    BLOCK_REASON_CLASSES,
    block_reason_class,
    gate_learning_summary,
    semantic_block_reason,
)
from runtime_session.lifecycle import (
    ActionPreparation,
    ActionPreflightResult,
    GateDecision,
    LifecycleController,
)
from runtime_session.projection import context_for


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

def _specs() -> list[dict]:
    """Kernel instructions mirroring the resolver shape (stage/frequency)."""
    return [
        {"stage": "owl_open", "frequency": "per_task",
         "execution_mode": "preflight", "activation_predicates": []},
        {"stage": "ward_enforce", "frequency": "per_action",
         "requires": ["fuse_plan"],
         "activation_predicates": [
             {"event": "action", "predicate": "action.mutating"}]},
        {"stage": "fuse_plan", "frequency": "per_action",
         "activation_predicates": [{"event": "action", "predicate": "action.any"}]},
        {"stage": "sispis_check", "frequency": "per_response"},
        {"stage": "flow_review", "frequency": "per_action"},
        {"stage": "anchor_closeout", "frequency": "per_response"},
    ]


def _bundle():
    return SimpleNamespace(kernel_instructions=_specs())


def _overlay(revision: int, modes: dict | None = None,
             expiries: dict | None = None,
             leases: tuple = ()) -> ComponentOverlay:
    modes = modes or {}
    expiries = expiries or {}
    controls = tuple(
        ComponentControl(
            component_id=component_id,
            mode=modes.get(component_id, COMPONENT_BASELINE_MODES[component_id]),
            scope=None,
            expires_at_action=expiries.get(component_id),
            source_revision=revision,
        )
        for component_id in sorted(COMPONENT_IDS)
    )
    return ComponentOverlay(
        behavior_revision=revision, behavior_mode="VERIFY",
        controls=controls, target_leases=tuple(leases))


def _lease(lease_id: str, target: str, applied: str, previous: str,
           expires: int, revision: int,
           recovery_epoch_id: str | None = None) -> AppliedControlLease:
    return AppliedControlLease(
        lease_id=lease_id, intervention_id=f"intervention-{lease_id}",
        target=target, applied_value=applied, previous_value=previous,
        expires_at_action=expires, behavior_revision=revision,
        recovery_epoch_id=recovery_epoch_id)


_STARTUP = {"owl_open", "ward_enforce", "fuse_plan"}


def _controller_with(overlay: ComponentOverlay) -> LifecycleController:
    controller = LifecycleController(_bundle())
    prepared = controller.prepare_component_overlay(overlay)
    controller.apply_component_overlay(prepared, startup_active=set(_STARTUP))
    return controller


# ---------------------------------------------------------------------------
# Lifecycle: expiry at the declared action boundary
# ---------------------------------------------------------------------------

def test_applied_component_control_expires_at_declared_boundary() -> None:
    controller = _controller_with(_overlay(1, {"owl": "elevated"}, {"owl": 5}))

    controller.synchronize_action_index(5)
    assert controller.component_mode("owl") == "elevated"

    controller.synchronize_action_index(6)
    assert controller.component_mode("owl") == COMPONENT_BASELINE_MODES["owl"]


def test_target_lease_expiry_preserves_unrelated_controls() -> None:
    leases = (
        _lease("lease-a", "control:verification_intensity", "high", "normal", 8, 1),
        _lease("lease-b", "control:exploration_budget", "bounded", "normal", 20, 1),
    )
    controller = _controller_with(_overlay(1, leases=leases))

    controller.synchronize_action_index(8)
    assert controller.expire_behavior_leases(8) is False
    values = controller.effective_behavior_target_values()
    assert values["control:verification_intensity"] == "high"
    assert values["control:exploration_budget"] == "bounded"

    assert controller.expire_behavior_leases(9) is True
    values = controller.effective_behavior_target_values()
    assert values["control:verification_intensity"] == "normal"
    assert values["control:exploration_budget"] == "bounded"

    # No new crossing on a repeated poll at the same index.
    assert controller.expire_behavior_leases(9) is False

    # Unrelated component controls never moved.
    assert controller.component_mode("owl") == COMPONENT_BASELINE_MODES["owl"]
    assert controller.component_mode("anchor") == COMPONENT_BASELINE_MODES["anchor"]


def test_action_boundary_clears_ephemeral_stages_but_keeps_task_scope() -> None:
    controller = _controller_with(_overlay(1))
    controller.on_task_start()
    controller.activate("ward_enforce", "test")
    assert "owl_open" in controller.active
    assert "ward_enforce" in controller.active

    controller.on_action_complete()

    assert controller.action_index == 1
    assert "ward_enforce" not in controller.active  # per_action cleared
    assert "owl_open" in controller.active  # per_task survives


def test_ward_dependencies_survive_unrelated_control_expiry() -> None:
    controller = _controller_with(
        _overlay(1, {"owl": "elevated", "fuse": "off"}, {"owl": 5}))
    controller.activate("ward_enforce", "test")
    assert "ward_enforce" in controller.active
    assert "fuse_plan" in controller.active

    # The owl control expires; ward enforcement and its fuse dependency do not.
    controller.synchronize_action_index(6)
    assert controller.component_mode("owl") == COMPONENT_BASELINE_MODES["owl"]
    assert "ward_enforce" in controller.active
    assert "fuse_plan" in controller.active

    # A suppressed fuse stage stays permitted only via its WARD dependency.
    controller.action_active.clear()
    controller.activate("fuse_plan", "retry")
    assert "fuse_plan" in controller.action_active
    assert controller.component_mode("fuse") == "off"

    # Suppression without a WARD dependency still hides the stage.
    suppressed = _controller_with(_overlay(1, {"owl": "off"}))
    suppressed.activate("owl_open", "test")
    assert "owl_open" not in suppressed.active


# ---------------------------------------------------------------------------
# Lifecycle: restart and concurrent-revision recovery
# ---------------------------------------------------------------------------

def test_restart_rebuilds_actuator_state_from_persisted_dp_state() -> None:
    leases = (_lease("lease-r", "control:verification_intensity",
                     "high", "normal", 10, 2),)
    overlay = _overlay(2, {"owl": "elevated"}, {"owl": 7}, leases)
    first = _controller_with(overlay)
    first.synchronize_action_index(6)
    first.activate("ward_enforce", "test")
    first.activate("owl_open", "test")
    persisted_state = first.capture_state()
    expected_active = set(first.active)
    expected_values = dict(first.effective_behavior_target_values())

    # Restart: a fresh controller rehydrates lifecycle state, the DP action
    # basis, and the persisted overlay.
    second = LifecycleController(_bundle())
    second.restore_state(persisted_state)
    second.synchronize_action_index(6)
    second.synchronize_component_overlay(overlay, startup_active=set(_STARTUP))

    assert set(second.active) == expected_active
    assert second.component_mode("owl") == "elevated"
    assert dict(second.effective_behavior_target_values()) == expected_values

    # Both controllers agree after the restart, including past an expiry.
    for controller in (first, second):
        controller.synchronize_action_index(8)
    assert first.component_mode("owl") == second.component_mode("owl") == "normal"
    assert (first.effective_behavior_target_values()
            == second.effective_behavior_target_values() == {
                "control:verification_intensity": "high"})


def test_concurrent_revision_conflict_fails_closed_then_recovers() -> None:
    controller = _controller_with(_overlay(1))
    controller.activate("owl_open", "test")
    pre_conflict_active = set(controller.active)

    prepared_a = controller.prepare_component_overlay(
        _overlay(2, {"owl": "elevated"}))
    prepared_b = controller.prepare_component_overlay(
        _overlay(2, {"owl": "passive"}))
    controller.apply_component_overlay(prepared_a, startup_active=set(_STARTUP))
    assert controller.component_mode("owl") == "elevated"

    # The concurrent writer loses: its basis moved underneath it.
    with pytest.raises(RuntimeError):
        controller.apply_component_overlay(prepared_b, startup_active=set(_STARTUP))

    # Recovery rolls the winner back, restoring the shared basis ...
    recovered = controller.rollback_component_overlay(
        prepared_a, expected_current_revision=2)
    assert controller.component_mode("owl") == COMPONENT_BASELINE_MODES["owl"]
    assert set(recovered) == pre_conflict_active

    # ... after which the other revision applies cleanly on the fresh basis.
    controller.apply_component_overlay(prepared_b, startup_active=set(_STARTUP))
    assert controller.component_mode("owl") == "passive"

    # Stale revisions never prepare in the first place.
    with pytest.raises(ValueError, match="stale"):
        controller.prepare_component_overlay(_overlay(0))


# ---------------------------------------------------------------------------
# Action gate: reason classes and bounded semantic reasons
# ---------------------------------------------------------------------------

def test_block_reason_classes_cover_the_gate_taxonomy() -> None:
    assert block_reason_class("ward.confirmation_required") == "authority"
    assert block_reason_class("dp.external_behavior_authority_unbound") == "authority"
    assert block_reason_class("csw.workflow_authority_unavailable") == "authority"
    assert block_reason_class("ward.authority_membership_stale") == "authority"
    assert block_reason_class("routing_policy.route_suppressed") == "policy"
    assert block_reason_class("ward.protected_resource") == "policy"
    assert block_reason_class("ward.secret_exposure") == "policy"
    assert block_reason_class("ward.untrusted_action") == "capability"
    assert block_reason_class("ward.capability_missing") == "capability"
    assert block_reason_class("owl.operator_contradiction") == "evidence"
    assert block_reason_class("owl.verification_action_limited") == "evidence"
    assert block_reason_class("flow.goal_progress_limit") == "evidence"

    # Every code the gate can emit has a class in the taxonomy.
    for code in BLOCK_REASON_CLASSES:
        assert block_reason_class(code) in {
            "authority", "policy", "evidence", "capability"}

    # Owner-prefix fallback for handler/hook-minted codes; safe default.
    assert block_reason_class("owl.custom_reinspect") == "evidence"
    assert block_reason_class("sispis.boundary_evidence") == "evidence"
    assert block_reason_class("cfw.tool_hook.block") == "policy"
    assert block_reason_class("unknown_code_without_owner") == "policy"
    assert block_reason_class("") == "policy"
    assert block_reason_class(None) == "policy"


def test_semantic_reason_is_bounded_and_argument_free() -> None:
    secret_path = "/home/operator/.ssh/id_ed25519"
    secret_token = "token-9f2c-secret"
    for code in BLOCK_REASON_CLASSES:
        text = semantic_block_reason(code)
        assert len(text) <= 160
        assert secret_path not in text
        assert secret_token not in text
        assert text.startswith("blocked: ")

    assert semantic_block_reason("cfw.action_policy_allow").startswith("allowed")

    hostile = 'ward.protected_resource"; DROP TABLE x; -- ' + "x" * 300
    text = semantic_block_reason(hostile)
    assert len(text) <= 160
    assert '"' not in text and ";" not in text

    assert len(semantic_block_reason("ward.confirmation_required", limit=32)) <= 32
    with pytest.raises(ValueError, match="out of bounds"):
        semantic_block_reason("ward.confirmation_required", limit=8)


def test_gate_results_record_reason_classes_for_dp_learning() -> None:
    descriptor = ActionDescriptor(
        "write_file", mutability="write", goal_relation="unrelated",
        affected_criterion_ids=("c1",))
    blocked = ActionPreparation(
        "action-request-1", GateDecision.BLOCK, descriptor, "digest-1",
        reason_codes=("ward.protected_resource",),
        reason_classes=("policy",))
    summary = gate_learning_summary(blocked)

    assert summary["decision"] == "block"
    assert summary["reason_codes"] == ["ward.protected_resource"]
    assert summary["reason_classes"] == ["policy"]
    assert summary["semantic_reason"] == semantic_block_reason(
        "ward.protected_resource")

    # The export carries no digests, request IDs, subjects, or arguments.
    exported = json.dumps(summary)
    assert "digest-1" not in exported
    assert "action-request-1" not in exported
    assert "write_file" not in exported
    assert set(summary) == {
        "decision", "reason_codes", "reason_classes", "semantic_reason"}
    assert len(summary["semantic_reason"]) <= 160

    # Results without recorded classes still classify deterministically.
    legacy = ActionPreflightResult(
        "digest-2", GateDecision.BLOCK, reason_codes=("owl.operator_contradiction",))
    assert legacy.reason_classes == ()
    legacy_summary = gate_learning_summary(legacy)
    assert legacy_summary["reason_classes"] == ["evidence"]

    allowed = ActionPreparation(
        "action-request-3", GateDecision.ALLOW, descriptor, "digest-3",
        reason_codes=("cfw.action_policy_allow",), reason_classes=())
    assert gate_learning_summary(allowed)["semantic_reason"].startswith("allowed")


# ---------------------------------------------------------------------------
# Projection: consequences of active controls only
# ---------------------------------------------------------------------------

_FORBIDDEN_ADMIN_MARKERS = (
    "behavior_revision",
    "source_revision",
    "lease_id",
    "intervention_id",
    "recovery_epoch_id",
    "expires_at_action",
    "applied_value",
    "previous_value",
    "target_leases",
    "framework_modes",
    "dp-runtime-",
    "policy_hash",
    "goal_contract_hash",
)


def _external_session():
    bundle = SimpleNamespace(
        pinned={"goal_contract": {}, "agent_id": "agent"},
        stateworks=[],
        kernel=["owl_open", "ward_enforce", "fuse_plan", "sispis_check", "flow_review"],
        guard_pack={"guards": [
            {"rule": "Verify before claiming completion.",
             "enforcement": {"mode": "prompt"}}]},
        handoff_plan=[{"producer": "seg-a", "packet": "packet-a"},
                      {"producer": "seg-b", "packet": "packet-b"}],
    )
    overlay = _overlay(
        2, {"owl": "elevated"},
        leases=(_lease("lease-9f2", "control:verification_intensity",
                       "high", "normal", 10, 2,
                       recovery_epoch_id="epoch-1"),))
    lifecycle = _controller_with(overlay)
    lifecycle.synchronize_action_index(3)
    lifecycle.on_task_start()
    lifecycle.activate("ward_enforce", "test")
    session = SimpleNamespace(
        require_external_behavior_projection=lambda boundary: None,
        bundle=bundle,
        behavior=SimpleNamespace(
            is_external=True,
            external_projection={"behavior_revision": 2, "focus_criteria": []}),
        lifecycle_workflow=SimpleNamespace(
            lifecycle=lifecycle,
            active_lifecycle=set(lifecycle.active),
            active_statework_ids=set(),
            current_segment_index=0),
        injected={
            "owl_open": "Owl is watching for verification gaps.",
            "ward_enforce": "WARD INTERNAL: deny everything silently",
            "fuse_plan": "FUSE HIDDEN MECHANISM: internal routing weights",
            "sispis_check": "SISPIS HIDDEN: completion internals",
            "flow_review": "FLOW HIDDEN: retry budget internals",
        },
        attempts=SimpleNamespace(controller=None),
    )
    return session


def _standalone_session():
    goal = {
        "objective": "Improve widget reliability",
        "deliverable_kind": "report",
        "work_mode": "advisory",
        "acceptance_criteria": [
            {"criterion_id": "c1", "claim": "widget documented", "required": True}],
        "non_goals": ["rewrite everything"],
    }
    bundle = SimpleNamespace(
        pinned={"goal_contract": goal, "goal_contract_hash": "h" * 64,
                "agent_id": "agent"},
        stateworks=[],
        kernel=["owl_open", "ward_enforce", "fuse_plan", "sispis_check"],
        guard_pack={"guards": [
            {"rule": "Verify before claiming completion.",
             "enforcement": {"mode": "prompt"}},
            {"rule": "Structural handler rule stays host-side.",
             "enforcement": {"mode": "structural", "handler": None}}]},
        handoff_plan=[{"producer": "seg-a", "packet": "packet-a"}],
    )
    lifecycle = LifecycleController(_bundle())
    lifecycle.on_task_start()
    lifecycle.activate("ward_enforce", "test")
    convergence = SimpleNamespace(
        compact_state_block=(
            lambda **kwargs: "FAILED PATHS (evidence only):\n"
                             "- cause c1 / approach deadbeef: "
                             "disproved by evidence ev-1\n"
                             "Current recovery epoch: 1/3\n"
                             "Required novelty: new causal hypothesis "
                             "backed by fresh evidence"))
    session = SimpleNamespace(
        require_external_behavior_projection=lambda boundary: None,
        bundle=bundle,
        behavior=SimpleNamespace(is_external=False),
        lifecycle_workflow=SimpleNamespace(
            lifecycle=lifecycle,
            active_lifecycle=set(lifecycle.active),
            active_statework_ids=set(),
            current_segment_index=0),
        injected={"owl_open": "Owl is watching for verification gaps.",
                  "ward_enforce": "WARD INTERNAL: deny everything silently"},
        attempts=SimpleNamespace(
            controller=SimpleNamespace(
                states=lambda: [convergence],
                max_recovery_epochs_without_progress=3)),
        _evaluate_standalone_behavior=lambda: SimpleNamespace(
            mode="DELIVER", reason="required criterion remains unfinished",
            active_criterion_ids=("c1",), goal_reanchor_required=False),
        _action_strategy_hint=lambda: "Preferred test route: read_first.",
    )
    return session


def test_external_projection_hides_admin_internals_and_hidden_stages() -> None:
    context = context_for(_external_session())

    for marker in _FORBIDDEN_ADMIN_MARKERS:
        assert marker not in context, marker
    assert "lease-9f2" not in context
    assert "intervention-lease-9f2" not in context
    assert "epoch-1" not in context

    # Hidden-visibility stages never appear, even with capsules injected.
    assert "===== OWL_OPEN =====" in context
    assert "WARD_ENFORCE" not in context
    assert "FUSE_PLAN" not in context
    assert "SISPIS_CHECK" not in context
    assert "FLOW_REVIEW" not in context
    assert "WARD INTERNAL" not in context
    assert "HIDDEN" not in context

    # Consequences of active controls remain: prompt guard rules and the
    # current-segment handoff only.
    assert "Verify before claiming completion." in context
    assert "packet-a" in context
    assert "packet-b" not in context


def test_standalone_projection_shows_consequences_not_mechanisms() -> None:
    context = context_for(_standalone_session())

    for marker in _FORBIDDEN_ADMIN_MARKERS:
        assert marker not in context, marker
    assert "h" * 64 not in context

    # Visible consequences: focused criteria, guard rules, convergence
    # memory, strategy hint, and the active pipeline of guidance stages.
    assert "c1: widget documented" in context
    assert "Verify before claiming completion." in context
    assert "Structural handler rule stays host-side." not in context
    assert "FAILED PATHS (evidence only):" in context
    assert "disproved by evidence ev-1" in context
    assert "Preferred test route: read_first." in context
    assert "ACTIVE PIPELINE: owl_open" in context
    assert "Non-goals: rewrite everything" in context

    # Hidden stages and their mechanisms stay out of the prompt.
    assert "ward_enforce" not in context
    assert "fuse_plan" not in context
    assert "WARD INTERNAL" not in context
