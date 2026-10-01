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
                            shape="quick"),
        telemetry_disabled=True,
    )
    adapter = task.digital_psychology_adapter()
    assert adapter.available_statework_ids() == tuple(sorted(
        str(item.get("id") or "") for item in task.bundle.stateworks))
    directive = {
        "directive_id": "dir-dp-test", "operation": "set_mode",
        "target": "mode", "value": "RECOVER", "cause": "equivalent_retry",
        "expected_effect": "Use a materially different approach.",
        "scope": "dp-runtime-" + "a" * 64, "expires_after_actions": 3,
        "intervention_id": "intv-dp-test",
    }
    prepared = adapter.prepare_behavior_directives((directive,))

    assert adapter.apply_behavior_directives(prepared) == ("dir-dp-test",)
    context = adapter.before_model_call("DP behavior projection", tool_capable=True)
    assert "APPLIED DIGITAL PSYCHOLOGY CONTROLS" in context
    assert "set_mode mode=RECOVER" in context
    assert "DP behavior projection" in context

    adapter.rollback_behavior_directives(prepared)
    restored = adapter.before_model_call("DP behavior projection")
    assert "set_mode mode=RECOVER" not in restored
    task.close()
