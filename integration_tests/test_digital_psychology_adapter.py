from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_resolver():
    spec = importlib.util.spec_from_file_location(
        "resolve_runtime", SCRIPTS / "resolve-runtime.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _projection(revision, *, cognitive_mode="DELIVER", flow_mode="off",
                owl_mode="normal", anchor_mode="normal", fuse_mode="normal",
                dox_mode="normal", focus=(), leases=(),
                action_index=0):
    return {
        "protocol_version": "2.1.0", "behavior_revision": revision,
        "host_action_index": action_index, "cognitive_mode": cognitive_mode,
        "framework_modes": {"owl": owl_mode, "anchor": anchor_mode,
                             "fuse": fuse_mode, "ward": "mandatory",
                             "flow": flow_mode, "sispis": "normal", "dox": dox_mode},
        "focus_criteria": list(focus), "verification_intensity": "normal",
        "exploration_budget": "normal", "retry_policy": "normal",
        "recovery_level": 0, "context_strategy": "standard",
        "response_strategy": "direct", "target_leases": list(leases),
    }


def test_task_handle_exposes_explicit_reversible_dp_component_port():
    resolve = _load_resolver()
    from cognitive_runtime import CognitiveRuntime

    task = CognitiveRuntime(telemetry_disabled=True).begin_task(
        resolve.TaskRequest(task_id="dp-component-port", application_id="dp-test",
                            shape="implement", subject_ref="repo:dp-component-port"),
        telemetry_disabled=True,
    )
    adapter = task.digital_psychology_adapter()
    assert adapter._session.behavior.is_external
    assert adapter._session.behavior.metacognitive_controller is None
    assert adapter._session.behavior.system_one_projector is None
    assert adapter._session.behavior.semantic_signal_store is None
    assert {"owl", "anchor", "fuse", "ward"}.issubset(
        adapter.available_component_ids())
    directive = {
        "directive_id": "dir-dp-test", "operation": "set_framework",
        "target": "owl", "value": "reset", "cause": "equivalent_retry",
        "expected_effect": "Suppress the stale reasoning component for one bounded attempt.",
        "scope": "dp-runtime-" + "a" * 64, "expires_after_actions": 3,
        "expires_at_action": 3,
        "intervention_id": "intv-dp-test",
    }
    fuse_directive = {
        **directive,
        "directive_id": "dir-dp-fuse-test",
        "target": "fuse",
        "value": "constrained",
    }
    prepared = adapter.prepare_behavior_directives((directive, fuse_directive))
    behavior_projection = _projection(
        1, cognitive_mode="RECOVER", owl_mode="reset", fuse_mode="constrained",
        leases=({"lease_id": "lease-owl", "intervention_id": "intv-dp-test",
                 "target": "framework:owl", "previous_value": "normal",
                 "applied_value": "reset", "expires_at_action": 3,
                 "source_revision": 1, "recovery_epoch_id": "epoch-dp-test"},
                {"lease_id": "lease-fuse", "intervention_id": "intv-dp-test",
                 "target": "framework:fuse", "previous_value": "normal",
                 "applied_value": "constrained", "expires_at_action": 3,
                 "source_revision": 1, "recovery_epoch_id": "epoch-dp-test"}))

    assert adapter.apply_behavior_projection(prepared, behavior_projection) == (
        "dir-dp-test", "dir-dp-fuse-test")
    assert adapter._session.behavior.external_projection[
        "framework_modes"]["fuse"] == "constrained"
    context = adapter.before_model_call("DP behavior projection", tool_capable=True)
    # Directive receipts and control-plane operation details stay host-only.
    assert "APPLIED DIGITAL PSYCHOLOGY CONTROLS" not in context
    assert "set_framework owl=reset" not in context
    assert "set_framework fuse=constrained" not in context
    assert "DP behavior projection" in context
    assert "ACTIVE PIPELINE: owl" not in context
    assert "ACTIVE PIPELINE:" not in context
    assert "Contract hash:" not in context
    assert "===== OWL =====" in context
    assert "===== FUSE =====" not in context
    assert "===== WARD =====" not in context
    strategy = adapter._session._action_strategy()
    assert "FUSE-constrained" in strategy["reason"]
    assert "FUSE is constrained" in adapter._session._action_strategy_hint()

    host_action = {"tool_binding_id": "read", "arguments": {"path": "src/view.py"},
                   "subject_ref": adapter._session.bundle.policy.subject_ref}
    preflight = adapter.preflight_action(host_action)
    authorized = adapter.authorize_action(host_action)
    assert preflight["decision"] == "allow"
    assert authorized["decision"] == "allow"
    assert authorized["action_digest"] == preflight["action_digest"]
    assert authorized["action_request_id"].startswith("action-request-")

    malformed = _projection(2, focus=("one",), leases=(
        {"lease_id": "lease-malformed", "intervention_id": "intv-dp-test",
         "target": "control:verification_intensity", "applied_value": "high",
         "expires_at_action": 4, "source_revision": 2},))
    try:
        adapter.apply_behavior_projection(
            adapter.prepare_behavior_directives(()), malformed)
    except ValueError as exc:
        assert "target lease" in str(exc)
    else:
        raise AssertionError("CFW accepted an attention lease wider than three criteria")

    adapter.rollback_behavior_directives(prepared)
    restored = adapter.before_model_call("DP behavior projection")
    assert "set_framework owl=reset" not in restored
    assert "===== OWL =====" in restored
    task.close()


def test_authority_identity_hashes_component_contract_semantics(monkeypatch):
    resolve = _load_resolver()
    from cognitive_runtime import CognitiveRuntime
    import digital_psychology_adapter as adapter_module
    from runtime_contract.component_control import COMPONENT_ALLOWED_MODES

    task = CognitiveRuntime(telemetry_disabled=True).begin_task(
        resolve.TaskRequest(task_id="dp-capability-identity", application_id="dp-test",
                            shape="implement", subject_ref="repo:dp-capability-identity"),
        telemetry_disabled=True,
    )
    try:
        adapter = task.digital_psychology_adapter()
        original = adapter.authority_identity()
        assert len(original["component_contract_hash"]) == 64
        assert len(original["capability_hash"]) == 64
        current = COMPONENT_ALLOWED_MODES["owl"]
        monkeypatch.setitem(COMPONENT_ALLOWED_MODES, "owl",
                            frozenset(current - {"reset"}))
        changed = adapter.authority_identity()
        assert changed["component_contract_hash"] != original["component_contract_hash"]
        assert changed["capability_hash"] != original["capability_hash"]
    finally:
        task.close()


def test_dp_component_overlay_changes_actual_flow_lifecycle_activation():
    resolve = _load_resolver()
    from cognitive_runtime import CognitiveRuntime

    task = CognitiveRuntime(telemetry_disabled=True).begin_task(
        resolve.TaskRequest(task_id="dp-flow-overlay", application_id="dp-test",
                            shape="performance", subject_ref="repo:dp-flow-overlay"),
        telemetry_disabled=True,
    )
    adapter = task.digital_psychology_adapter()
    assert "flow" in adapter.available_component_ids()

    def state(revision: int, flow_mode: str) -> dict[str, object]:
        return _projection(revision, flow_mode=flow_mode)

    runtime_session = adapter._session
    contract = adapter.task_behavior_contract()
    assert contract["baseline_floors"]["flow"] == "normal"
    assert contract["suppression_modes"]["flow"] == ["off", "passive"]
    runtime_session.apply_external_behavior_projection(state(1, "normal"))
    try:
        runtime_session.apply_external_behavior_projection(state(2, "off"))
    except PermissionError as exc:
        assert "FLOW task baseline floor" in str(exc)
    else:
        raise AssertionError("CFW accepted a DP projection below the FLOW task floor")

    runtime_session.apply_external_behavior_projection(state(2, "elevated"))
    runtime_session.on_artifact(flow_triggered=False)
    assert runtime_session.lifecycle_workflow.lifecycle.counts.get("flow", 0) == 1
    elevated_context = task.before_model_call()
    assert "flow" in runtime_session.lifecycle_workflow.lifecycle.active
    assert "===== FLOW =====" not in elevated_context
    assert adapter._session.injected["flow"] not in elevated_context

    runtime_session.rollback_external_behavior_projection(
        state(1, "normal"), expected_current_revision=2)
    assert runtime_session.lifecycle_workflow.lifecycle.component_mode("flow") == "normal"
    assert "flow" not in runtime_session.lifecycle_workflow.lifecycle.active

    runtime_session.synchronize_external_behavior_projection(state(3, "elevated"))
    runtime_session.on_artifact(flow_triggered=False)
    assert runtime_session.lifecycle_workflow.lifecycle.counts.get("flow", 0) == 1
    task.close()


def test_dp_cannot_suppress_components_selected_by_performance_or_doc_edit():
    """DP's first projection must preserve each resolved CFW task baseline."""
    resolve = _load_resolver()
    from cognitive_runtime import CognitiveRuntime

    performance = CognitiveRuntime(telemetry_disabled=True).begin_task(
        resolve.TaskRequest(task_id="dp-floor-performance", application_id="dp-test",
                            shape="performance", subject_ref="repo:dp-floor-performance"),
        telemetry_disabled=True,
    )
    try:
        adapter = performance.digital_psychology_adapter()
        contract = adapter.task_behavior_contract()
        assert contract["baseline_floors"]["flow"] == "normal"
        session = adapter._session
        session.apply_external_behavior_projection(_projection(1, flow_mode="normal"))
        session.on_artifact(flow_triggered=True)
        assert "flow" in session.lifecycle_workflow.lifecycle.active
        try:
            session.apply_external_behavior_projection(_projection(2, flow_mode="off"))
        except PermissionError as exc:
            assert "FLOW task baseline floor" in str(exc)
        else:
            raise AssertionError("DP suppressed FLOW selected by the performance profile")
    finally:
        performance.close()

    documentation = CognitiveRuntime(telemetry_disabled=True).begin_task(
        resolve.TaskRequest(task_id="dp-floor-doc-edit", application_id="dp-test",
                            shape="doc_edit", subject_ref="repo:dp-floor-doc-edit"),
        telemetry_disabled=True,
    )
    try:
        adapter = documentation.digital_psychology_adapter()
        contract = adapter.task_behavior_contract()
        assert contract["baseline_floors"]["anchor"] == "normal"
        assert contract["baseline_floors"]["dox"] == "normal"
        session = adapter._session
        baseline = _projection(1, anchor_mode="normal", dox_mode="normal")
        session.apply_external_behavior_projection(baseline)
        session.on_artifact(durable_contract_changed=True, state_changed=True)
        assert {"anchor_closeout", "dox_closeout"}.issubset(
            session.lifecycle_workflow.lifecycle.active)
        for field, component, suppressed in (
                ("anchor_mode", "ANCHOR", "passive"),
                ("dox_mode", "DOX", "off")):
            projection = _projection(
                2, anchor_mode=(suppressed if field == "anchor_mode" else "normal"),
                dox_mode=(suppressed if field == "dox_mode" else "normal"))
            try:
                session.apply_external_behavior_projection(projection)
            except PermissionError as exc:
                assert f"{component} task baseline floor" in str(exc)
            else:
                raise AssertionError(
                    f"DP lowered the {component} floor selected by doc_edit")
    finally:
        documentation.close()


def test_dp_temporary_component_control_expires_without_a_dp_tick():
    resolve = _load_resolver()
    from cognitive_runtime import CognitiveRuntime

    runtime = CognitiveRuntime(telemetry_disabled=True)
    task = runtime.begin_task(
        resolve.TaskRequest(task_id="dp-flow-local-expiry", application_id="dp-test",
                            shape="performance", subject_ref="repo:dp-flow-local-expiry"),
        telemetry_disabled=True,
    )
    adapter = task.digital_psychology_adapter()
    runtime_session = adapter._session
    state = _projection(1, leases=({
        "lease_id": "lease-flow", "intervention_id": "intv-flow-local-expiry",
        "target": "framework:flow", "previous_value": "normal",
        "applied_value": "elevated", "expires_at_action": 0,
        "source_revision": 1, "recovery_epoch_id": "epoch-flow-expiry"},))
    state["framework_modes"]["flow"] = "elevated"

    runtime_session.apply_external_behavior_projection(state)
    assert runtime_session.lifecycle_workflow.lifecycle.component_mode("flow") == "elevated"

    # The host's local action boundary expires the overlay even if DP is gone.
    runtime.host_handle(task).complete_action()
    assert runtime_session.lifecycle_workflow.lifecycle.action_index == 1
    assert runtime_session.lifecycle_workflow.lifecycle.component_mode("flow") == "normal"
    assert runtime_session.lifecycle_workflow.lifecycle.component_mode("ward") == "mandatory"
    runtime_session.on_artifact(flow_triggered=True)
    assert "flow" in runtime_session.lifecycle_workflow.lifecycle.active
    task.close()


def test_dp_reset_mode_reexecutes_the_selected_component_stage():
    resolve = _load_resolver()
    from cognitive_runtime import CognitiveRuntime

    task = CognitiveRuntime(telemetry_disabled=True).begin_task(
        resolve.TaskRequest(task_id="dp-component-reset", application_id="dp-test",
                            shape="implement", subject_ref="repo:dp-component-reset"),
        telemetry_disabled=True,
    )
    adapter = task.digital_psychology_adapter()

    def state(revision: int, owl_mode: str) -> dict[str, object]:
        return _projection(revision, cognitive_mode="RECOVER", owl_mode=owl_mode)

    runtime_session = adapter._session
    runtime_session.apply_external_behavior_projection(state(1, "normal"))
    runtime_session.apply_external_behavior_projection(state(2, "reset"))
    assert "owl" in runtime_session.lifecycle_workflow.lifecycle.active
    assert runtime_session.lifecycle_workflow.lifecycle.counts.get("owl") == 1
    task.close()


def test_cfw_criterion_ledger_keeps_more_than_eight_items_and_rejects_overflow():
    from runtime_contract.metacognition import MetacognitiveState

    state = MetacognitiveState()
    state.ensure_criteria({"acceptance_criteria": [{
        "criterion_id": f"criterion-{index}", "claim": f"Claim {index}",
        "kind": "functional",
    } for index in range(12)]})
    assert len(state.criteria) == 12

    try:
        state.ensure_criteria({"acceptance_criteria": [{
            "criterion_id": f"overflow-{index}", "claim": f"Claim {index}",
            "kind": "functional",
        } for index in range(257)]})
    except ValueError as exc:
        assert "256-criterion" in str(exc)
    else:
        raise AssertionError("CFW accepted a task set beyond the runtime contract")
