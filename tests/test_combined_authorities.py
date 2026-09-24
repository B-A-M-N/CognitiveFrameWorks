"""Cross-project authority and adaptive-action acceptance proofs."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COW = Path("/home/bamn/CognitiveStateWorks") if Path("/home/bamn/CognitiveStateWorks").exists() else Path("/home/bamn/CognitiveStateWork")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_modules():
    sys.modules.pop("resolve_runtime", None)
    resolve = _load("combined_resolve", ROOT / "scripts" / "resolve-runtime.py")
    sys.modules["resolve_runtime"] = resolve
    runtime = _load("combined_runtime", ROOT / "scripts" / "runtime.py")
    return resolve, runtime


def test_independent_cfw_tasks_share_csw_subject_revision(tmp_path):
    resolve, runtime = _runtime_modules()
    authority = runtime.CSWStateAuthority(
        COW, state_path=tmp_path / "subjects.json", evidence_path=tmp_path / "evidence.json"
    )
    for task_id, output_state, trigger, evidence_type in (
        ("task-one", "OBSERVED", "discover", "rendered_observation"),
        ("task-two", "MODELED", "model", "state_model"),
    ):
        request = resolve.TaskRequest(
            task_id=task_id, application_id="combined-test", subject_ref="repo:shared",
            shape="quick", domain_tags=("terminal interface",),
        )
        bundle = resolve.resolve(request, cow_root=COW, requested_flows={"tuid": "discover"})
        session = runtime.start(bundle, telemetry_disabled=True, state_authority=authority)
        host = session._host_ingress()
        evidence = host.record_observation(evidence_type, subject_ref="repo:shared")
        assert session.record_state_transition(
            "tuid", "repo:shared", output_state, trigger=trigger,
            evidence_refs=[evidence]) == runtime.GateDecision.ALLOW
    snapshot = authority.inspect(
        statework_id="tuid", subject_ref="repo:shared",
        contract_hash=__import__("hashlib").sha256(
            __import__("json").dumps(
                __import__("yaml").safe_load((COW / "tuid" / "transitions.yaml").read_text()),
                sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
    )
    assert snapshot["state"] == "MODELED"
    # revision 1 is durable initialization, then two independently recorded transitions
    assert snapshot["revision"] == 3


def test_action_preference_orders_but_does_not_authorize(tmp_path, monkeypatch):
    resolve, runtime = _runtime_modules()
    resolve.runtime_state_dir = lambda: tmp_path / "runtime"

    class Advisor:
        def advice_for_task(self, **kwargs):
            return {"status": "ok", "adjustments": [{
                "route_type": "tool_policy", "route": "read", "disposition": "prefer"
            }]}

    request = resolve.TaskRequest(task_id="action-pref", application_id="combined-test", shape="quick")
    bundle = resolve.resolve(request, cow_root=COW, behavioral_advisor=Advisor())
    session = runtime.start(bundle, telemetry_disabled=True)
    prompt = session.before_model_call(tool_capable=True)
    strategy = session._action_strategy()
    assert strategy["eligible_actions"][0] == "read"
    assert "ACTION STRATEGY" in prompt
    assert session.before_action({"tool_binding_id": "read", "arguments": {}}) == runtime.GateDecision.ALLOW
    assert session.before_action({"tool_binding_id": "shell", "arguments": {}}) != runtime.GateDecision.ALLOW
