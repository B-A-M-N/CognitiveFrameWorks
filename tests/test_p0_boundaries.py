import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _resolver():
    spec = importlib.util.spec_from_file_location("boundary_resolver", SCRIPTS / "resolve-runtime.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WardSuppressor:
    def advice_for_task(self, **kwargs):
        return {"status": "ok", "adjustments": [{"route_type": "stage", "route": "ward",
                                 "disposition": "suppress"}]}


def test_advisor_cannot_bypass_mandatory_stage():
    resolve = _resolver()
    with pytest.raises(Exception, match="mandatory stage"):
        resolve.resolve(resolve.TaskRequest(task_id="p0", application_id="app", shape="implement"),
                        behavioral_advisor=WardSuppressor())


def test_cfw_only_uses_empty_statework_capability(tmp_path):
    resolve = _resolver()
    bundle = resolve.resolve(resolve.TaskRequest(task_id="standalone", application_id="app", shape="quick"),
                             cow_root=tmp_path / "absent")
    assert bundle.stateworks == ()
    assert bundle.kernel == ("sispis",)


def test_explicit_ineligible_flow_is_rejected():
    resolve = _resolver()
    cow = Path("/home/bamn/CognitiveStateWorks") if Path("/home/bamn/CognitiveStateWorks").exists() else Path("/home/bamn/CognitiveStateWork")
    stateworks = resolve.load_stateworks(cow)
    tuid = next(item for item in stateworks if item["id"] == "tuid")
    try:
        resolve.select_statework_runtime(tuid, cow, "debug", operation="mutate",
                                          task_shape="quick")
    except Exception as exc:
        assert "not eligible" in str(exc) or "no flow matching" in str(exc)
    else:
        raise AssertionError("explicit ineligible flow was accepted")

def test_advisor_receives_host_eligible_statework_flow_choices():
    resolve = _resolver()
    seen = {}
    class Advisor:
        def advice_for_task(self, **kwargs):
            seen.update(kwargs)
            return {"status": "ok", "adjustments": []}
    cow = Path("/home/bamn/CognitiveStateWorks") if Path("/home/bamn/CognitiveStateWorks").exists() else Path("/home/bamn/CognitiveStateWork")
    request = resolve.TaskRequest(task_id="flow-advice", application_id="app",
                                  shape="quick", domain_tags=("infrastructure",),
                                  operation="deploy", subject_ref="repo")
    resolve.resolve(request, cow_root=cow, behavioral_advisor=Advisor())
    routes = seen["eligible_routes"]
    assert any(item["route_type"] == "statework" and item["route"] == "infrae" for item in routes)
    assert any(item["route_type"] == "flow" and item["statework_id"] == "infrae" for item in routes)

def test_legal_flow_preference_changes_selection():
    resolve = _resolver()
    cow = Path("/home/bamn/CognitiveStateWorks") if Path("/home/bamn/CognitiveStateWorks").exists() else Path("/home/bamn/CognitiveStateWork")
    class Advisor:
        def __init__(self, route): self.route = route
        def advice_for_task(self, **kwargs):
            return {"status": "ok", "adjustments": [{
                "route_type": "flow", "route": self.route,
                "statework_id": "infrae", "disposition": "prefer",
            }]}
    request = resolve.TaskRequest(task_id="flow-pref", application_id="app",
                                  subject_ref="infra", shape="implement",
                                  domain_tags=("infrastructure",), operation="deploy")
    bundle = resolve.resolve(request, cow_root=cow, behavioral_advisor=Advisor("change"))
    assert Path(bundle.stateworks[0]["selected_flow"][0]).parent.name == "change"
    try:
        resolve.resolve(request, cow_root=cow, behavioral_advisor=Advisor("not-a-flow"))
    except ValueError as exc:
        assert "ineligible" in str(exc)
    else:
        raise AssertionError("ineligible learned flow was accepted")

def test_advisor_sees_alternative_eligible_flows():
    resolve = _resolver()
    cow = Path("/home/bamn/CognitiveStateWorks") if Path("/home/bamn/CognitiveStateWorks").exists() else Path("/home/bamn/CognitiveStateWork")
    infrae = next(item for item in resolve.load_stateworks(cow) if item["id"] == "infrae")
    routes = resolve.enumerate_eligible_routes(infrae, cow, operation="deploy", task_shape="implement")
    assert "change" in {route["route"] for route in routes if route["route_type"] == "flow"}
    assert all(route["route"] != "debug" for route in routes if route["route_type"] == "flow")
