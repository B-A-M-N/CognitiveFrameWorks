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


def test_task_handle_exposes_explicit_reversible_dp_component_port():
    resolve = _load_resolver()
    from cognitive_runtime import CognitiveRuntime

    task = CognitiveRuntime(telemetry_disabled=True).begin_task(
        resolve.TaskRequest(task_id="dp-component-port", application_id="dp-test",
                            shape="implement", subject_ref="repo:dp-component-port"),
        telemetry_disabled=True,
    )
    adapter = task.digital_psychology_adapter()
    assert {"owl", "anchor", "fuse", "ward"}.issubset(
        adapter.available_component_ids())
    directive = {
        "directive_id": "dir-dp-test", "operation": "set_framework",
        "target": "owl", "value": "off", "cause": "equivalent_retry",
        "expected_effect": "Suppress the stale reasoning component for one bounded attempt.",
        "scope": "dp-runtime-" + "a" * 64, "expires_after_actions": 3,
        "intervention_id": "intv-dp-test",
    }
    fuse_directive = {
        **directive,
        "directive_id": "dir-dp-fuse-test",
        "target": "fuse",
        "value": "constrained",
    }
    prepared = adapter.prepare_behavior_directives((directive, fuse_directive))
    behavior_state = {
        "revision": 1,
        "cognitive_mode": "RECOVER",
        "framework_modes": {"owl": "off", "anchor": "passive", "fuse": "constrained",
                             "ward": "mandatory", "flow": "off", "sispis": "normal",
                             "dox": "off"},
        "active_stateworks": [], "focus_criteria": [],
        "attention_lease": None, "attention_last_seen": {},
        "verification_intensity": "normal", "exploration_budget": "normal",
        "retry_policy": "normal", "recovery_level": 0,
        "context_strategy": "standard", "response_strategy": "direct",
        "active_interventions": [], "intervention_history": [],
    }

    assert adapter.apply_behavior_directives(prepared, behavior_state) == (
        "dir-dp-test", "dir-dp-fuse-test")
    assert adapter._session.external_behavior_state["framework_modes"]["fuse"] == "constrained"
    context = adapter.before_model_call("DP behavior projection", tool_capable=True)
    # Directive receipts and control-plane operation details stay host-only.
    assert "APPLIED DIGITAL PSYCHOLOGY CONTROLS" not in context
    assert "set_framework owl=off" not in context
    assert "set_framework fuse=constrained" not in context
    assert "DP behavior projection" in context
    assert "ACTIVE PIPELINE: owl" not in context
    assert "===== OWL =====" not in context
    strategy = adapter._session._action_strategy()
    assert "FUSE-constrained" in strategy["reason"]
    assert "FUSE is constrained" in adapter._session._action_strategy_hint()

    malformed = dict(behavior_state)
    malformed["revision"] = 2
    malformed["focus_criteria"] = ["one", "two", "three", "four"]
    malformed["attention_lease"] = {
        "lease_id": "attn-test", "criterion_ids": malformed["focus_criteria"],
        "issued_revision": 2, "goal_set_revision": 1, "start_action": 1,
        "max_actions": 3, "reason": "test",
    }
    try:
        adapter._session.apply_external_behavior_state(malformed)
    except ValueError as exc:
        assert "attention lease" in str(exc)
    else:
        raise AssertionError("CFW accepted an attention lease wider than three criteria")

    adapter.rollback_behavior_directives(prepared)
    restored = adapter.before_model_call("DP behavior projection")
    assert "set_framework owl=off" not in restored
    assert "===== OWL =====" in restored
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
        return {
            "revision": revision,
            "cognitive_mode": "DELIVER",
            "framework_modes": {"owl": "normal", "anchor": "passive",
                                 "fuse": "normal", "ward": "mandatory",
                                 "flow": flow_mode, "sispis": "normal", "dox": "off"},
            "active_stateworks": [], "focus_criteria": [],
            "attention_lease": None, "attention_last_seen": {},
            "verification_intensity": "normal", "exploration_budget": "normal",
            "retry_policy": "normal", "recovery_level": 0,
            "context_strategy": "standard", "response_strategy": "direct",
            "active_interventions": [], "intervention_history": [],
        }

    runtime_session = adapter._session
    runtime_session.apply_external_behavior_state(state(1, "off"))
    runtime_session.on_artifact(flow_triggered=True)
    off_context = task.before_model_call()
    assert "flow" not in runtime_session.lifecycle.active
    assert runtime_session.lifecycle.counts.get("flow", 0) == 0
    assert "===== FLOW =====" not in off_context

    runtime_session.apply_external_behavior_state(state(2, "elevated"))
    runtime_session.on_artifact(flow_triggered=False)
    assert runtime_session.lifecycle.counts.get("flow", 0) == 1
    elevated_context = task.before_model_call()
    assert "flow" in runtime_session.lifecycle.active
    assert "===== FLOW =====" in elevated_context
    assert adapter._session.injected["flow"] in elevated_context

    runtime_session.rollback_external_behavior_state(
        state(1, "off"), expected_current_revision=2)
    assert runtime_session.lifecycle.component_mode("flow") == "off"
    assert "flow" not in runtime_session.lifecycle.active

    runtime_session.synchronize_external_behavior_state(state(3, "elevated"))
    runtime_session.on_artifact(flow_triggered=False)
    assert runtime_session.lifecycle.counts.get("flow", 0) == 1
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
        return {
            "revision": revision,
            "cognitive_mode": "RECOVER",
            "framework_modes": {"owl": owl_mode, "anchor": "normal",
                                 "fuse": "normal", "ward": "mandatory",
                                 "flow": "off", "sispis": "normal", "dox": "off"},
            "active_stateworks": [], "focus_criteria": [],
            "attention_lease": None, "attention_last_seen": {},
            "verification_intensity": "normal", "exploration_budget": "normal",
            "retry_policy": "normal", "recovery_level": 0,
            "context_strategy": "standard", "response_strategy": "direct",
            "active_interventions": [], "intervention_history": [],
        }

    runtime_session = adapter._session
    runtime_session.apply_external_behavior_state(state(1, "off"))
    assert "owl" not in runtime_session.lifecycle.active
    runtime_session.apply_external_behavior_state(state(2, "reset"))
    assert "owl" in runtime_session.lifecycle.active
    assert runtime_session.lifecycle.counts.get("owl") == 1
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
