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
    snapshot = session.state_authority.inspect(
        statework_id="tuid", subject_ref="repo:shared",
        contract_hash=__import__("hashlib").sha256(
            __import__("json").dumps(
                __import__("yaml").safe_load((COW / "tuid" / "transitions.yaml").read_text()),
                sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
    )
    assert snapshot["state"] == "MODELED"
    # revision 1 is durable initialization, then two independently recorded transitions
    assert snapshot["revision"] == 3


def test_action_preference_orders_but_does_not_authorize():
    resolve, runtime = _runtime_modules()

    class Advisor:
        def advice_for_task(self, *, static_policy, **kwargs):
            return {**static_policy, "status": "ok", "adjustments": [{
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


def test_combined_enforcing_requires_shared_authority():
    resolve, runtime = _runtime_modules()
    request = resolve.TaskRequest(
        task_id="requires-authority", namespace_id="ns", application_id="app",
        application_instance_id="app-instance", subject_ref="repo:shared",
        shape="quick", domain_tags=("terminal interface",),
    )
    bundle = resolve.resolve(request, cow_root=COW, requested_flows={"tuid": "discover"})
    with pytest.raises(ValueError, match="injected shared StateAuthority"):
        runtime.start(bundle, telemetry_disabled=True, runtime_mode="combined_enforcing")


def test_shared_authority_identity_uses_task_origin(tmp_path):
    resolve, runtime = _runtime_modules()
    request = resolve.TaskRequest(
        task_id="origin-bound", namespace_id="tenant-a", application_id="app-a",
        application_instance_id="instance-a", subject_ref="repo:shared",
        shape="quick", domain_tags=("terminal interface",),
    )
    bundle = resolve.resolve(request, cow_root=COW, requested_flows={"tuid": "discover"})
    authority = runtime.CSWStateAuthority(
        COW, state_path=tmp_path / "subjects.json", evidence_path=tmp_path / "evidence.json")
    session = runtime.start(bundle, telemetry_disabled=True, runtime_mode="combined_enforcing", state_authority=authority)
    snapshot = authority.inspect(
        statework_id="tuid", subject_ref="repo:shared",
        contract_hash=__import__("hashlib").sha256(__import__("json").dumps(
            __import__("yaml").safe_load((COW / "tuid" / "transitions.yaml").read_text()),
            sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest())
    assert snapshot["state"] == "UNKNOWN"
    assert session.state_authority.namespace_id == "tenant-a"
    assert session.state_authority.application_id == "app-a"
    assert session.state_authority.application_instance_id == "instance-a"
    assert authority.namespace_id == "default"
    assert session.runtime_mode == "combined_enforcing"


def test_authority_unavailable_blocks_consequential_action(tmp_path):
    resolve, runtime = _runtime_modules()

    class InterruptibleAuthority:
        unavailable = False

        def inspect(self, **_kwargs):
            if self.unavailable:
                raise RuntimeError("offline")
            return {"state": "UNKNOWN", "revision": 0}

        def commit(self, **_kwargs):
            if self.unavailable:
                raise RuntimeError("offline")
            return {"state": "UNKNOWN", "revision": 0}

    request = resolve.TaskRequest(
        task_id="authority-loss", namespace_id="ns", application_id="app",
        application_instance_id="instance", subject_ref="repo:shared",
        shape="quick", domain_tags=("terminal interface",),
    )
    bundle = resolve.resolve(request, cow_root=COW, requested_flows={"tuid": "discover"})
    authority = InterruptibleAuthority()
    session = runtime.start(bundle, telemetry_disabled=True, runtime_mode="combined_enforcing", state_authority=authority)
    authority.unavailable = True
    assert session.before_action({"tool_binding_id": "read", "arguments": {}, "subject_ref": "repo:shared"}) == runtime.GateDecision.BLOCK


def test_task_handle_does_not_expose_host_authority():
    resolve, runtime = _runtime_modules()
    api = runtime.cognitive_runtime.CognitiveRuntime(telemetry_disabled=True) if hasattr(runtime, "cognitive_runtime") else None
    # Import through the normal module path used by public API tests.
    sys.modules.pop("cognitive_runtime", None)
    import importlib.util
    spec = importlib.util.spec_from_file_location("cognitive_runtime", ROOT / "scripts" / "cognitive_runtime.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.resolve_runtime = resolve
    module.start = runtime.start
    request = resolve.TaskRequest(task_id="no-host-handle", application_id="app", shape="quick")
    task = module.CognitiveRuntime(telemetry_disabled=True).begin_task(request)
    assert not hasattr(task, "host")
    host = module.CognitiveRuntime(telemetry_disabled=True).host_handle(task)
    assert hasattr(host, "request_transition")


def test_shared_authority_scope_is_immutable_and_task_views_do_not_mutate_parent(tmp_path):
    from datetime import datetime, timedelta, timezone
    resolve, runtime = _runtime_modules()
    authority = runtime.CSWStateAuthority(
        COW, state_path=tmp_path / "subjects.json", evidence_path=tmp_path / "evidence.json",
        namespace_id="host-service", application_id="service", application_instance_id="service",
        workflow_id="service-workflow")
    membership_authority = runtime.SharedWorkflowMembershipAuthority(b"m" * 32)
    def request(task, namespace, instance):
        grant = membership_authority.issue(
            namespace_id=namespace, application_id="app",
            application_instance_id=instance, workflow_id="workflow", statework_id="tuid",
            subject_ref="repo:shared", member_agent_id="agent",
            allowed_operations=["transition"],
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            membership_revision=1)
        return resolve.TaskRequest(
            task_id=task, application_id="app", application_instance_id=instance,
            namespace_id=namespace, subject_ref="repo:shared", shape="quick",
            domain_tags=("terminal interface",), phase="domain_state",
            shared_workflow_membership=grant)
    a = resolve.resolve(request("task-a", "ns-a", "instance-a"), requested_flows={"tuid": "discover"})
    b = resolve.resolve(request("task-b", "ns-b", "instance-b"), requested_flows={"tuid": "discover"})
    session_a = runtime.start(a, telemetry_disabled=True, state_authority=authority,
                              runtime_mode="combined_enforcing",
                              membership_authority=membership_authority)
    session_b = runtime.start(b, telemetry_disabled=True, state_authority=authority,
                              runtime_mode="combined_enforcing",
                              membership_authority=membership_authority)
    assert authority.workflow_id == "service-workflow"
    assert session_a.state_authority.workflow_id == "workflow"
    assert session_b.state_authority.workflow_id == "workflow"
    assert session_a.state_authority is not session_b.state_authority
    assert session_a.state_authority.state_store.path == session_b.state_authority.state_store.path
    assert session_a.state_authority.evidence_path == session_b.state_authority.evidence_path
    try:
        authority.workflow_id = "mutated"
    except AttributeError:
        pass
    else:
        raise AssertionError("shared authority scope mutated")


def test_unauthorized_membership_transition_is_rejected_without_mutation(tmp_path):
    resolve, runtime = _runtime_modules()
    authority = runtime.CSWStateAuthority(
        COW, state_path=tmp_path / "subjects.json", evidence_path=tmp_path / "evidence.json")
    membership_authority = runtime.SharedWorkflowMembershipAuthority(b"m" * 32)
    forged = {
        "namespace_id": "ns", "application_id": "app", "application_instance_id": "instance",
        "workflow_id": "workflow", "statework_id": "tuid", "subject_ref": "repo:shared",
        "member_agent_id": "agent", "allowed_operations": ["transition"],
        "expires_at": "2099-01-01T00:00:00Z", "membership_revision": 1,
        "grant_id": "forged", "signature": "forged",
    }
    request = resolve.TaskRequest(
        task_id="forged", application_id="app", application_instance_id="instance",
        namespace_id="ns", subject_ref="repo:shared", shape="quick",
        domain_tags=("terminal interface",), phase="domain_state",
        shared_workflow_membership=forged)
    bundle = resolve.resolve(request, requested_flows={"tuid": "discover"})
    with pytest.raises(PermissionError, match="not host-authorized"):
        runtime.start(bundle, telemetry_disabled=True, state_authority=authority,
                      runtime_mode="combined_enforcing",
                      membership_authority=membership_authority)
    state = json.loads((tmp_path / "subjects.json").read_text()) if (tmp_path / "subjects.json").exists() else {"subjects": {}}
    assert not any(item["subject_ref"] == "repo:shared" and item["state"] == "OBSERVED"
                   for item in state.get("subjects", {}).values())
