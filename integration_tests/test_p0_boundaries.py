import importlib.util
import sys
from pathlib import Path

import pytest
from runtime_contract.statework_provider import StateWorkProviderError
from runtime_contract.statework_provider import default_statework_root
from runtime_contract.workflow_port import (
    WorkflowEligibility, WorkflowPort, WorkflowResolution, WorkflowRouteRanking,
    validate_workflow_port,
)
from runtime_contract.component_control import COMPONENT_IDS, ComponentOverlay

ROOT = Path(__file__).resolve().parents[1]
COW = ROOT.parent / "CognitiveStateWork"
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
    def advice_for_task(self, *, static_policy, **kwargs):
        return {**static_policy, "status": "ok", "adjustments": [{"route_type": "stage", "route": "ward",
                                 "disposition": "suppress"}]}


def test_advisor_cannot_bypass_mandatory_stage():
    resolve = _resolver()
    with pytest.raises(Exception, match="mandatory stage"):
        resolve.resolve(resolve.TaskRequest(task_id="p0", application_id="app", shape="implement"),
                        behavioral_advisor=WardSuppressor())


def test_cfw_only_uses_empty_statework_capability():
    resolve = _resolver()
    before = {name for name in sys.modules if name.startswith("CognitiveStateWork")}
    bundle = resolve.resolve(resolve.TaskRequest(
        task_id="standalone", application_id="app", shape="quick"))
    assert bundle.stateworks == ()
    assert bundle.kernel == ("sispis",)
    after = {name for name in sys.modules if name.startswith("CognitiveStateWork")}
    assert after == before


def test_statework_provider_contract_and_fail_closed_configured_root(tmp_path):
    resolve = _resolver()
    provider = resolve.statework_provider(tmp_path / "absent")
    assert provider.load_stateworks() == []
    assert provider.packet_registry() == {}
    for method in ("packet_store", "bundle_root", "validate_flow_contracts",
                   "flow_metadata", "flow_selector", "create_state_authority"):
        assert callable(getattr(provider, method))
    with pytest.raises(StateWorkProviderError, match="unavailable without an installed provider"):
        provider.create_state_authority()

    invalid = tmp_path / "invalid-csw"
    invalid.mkdir()
    (invalid / "registry.yaml").write_text("version: 1.0.0\n", encoding="utf-8")
    with pytest.raises(resolve.PacketDependencyError, match="compiled StateWork registry is invalid"):
        resolve.load_stateworks(invalid)


def test_cfw_does_not_discover_optional_provider_during_default_startup(monkeypatch):
    # Even when an optional CSW entry point is installed, startup defaults to
    # the CFW-owned registry location. Hosts opt in by supplying a root/port.
    monkeypatch.delenv("COGNITIVE_STATEWORK_ROOT", raising=False)
    assert default_statework_root() == ROOT / "statework"


def test_neutral_workflow_port_is_structural_and_engine_agnostic():
    class HostWorkflows:
        def snapshot(self, *, runtime_identity, task_context):
            return {"revision": 1}

        def active_guidance(self, *, runtime_identity, task_context):
            return {"revision": 1, "guidance": []}

        def apply_step(self, *, runtime_identity, step):
            return {"accepted": True}

        def eligible_routes(self, *, runtime_identity, task_context):
            return WorkflowEligibility(eligible_routes=())

        def resolve_workflows(self, *, runtime_identity, task_context,
                              routing_adjustments):
            return WorkflowResolution(
                execution_order=(), active_workflows=(), capsules={},
                component_requirements=(), component_enhancements=(),
                registry_version="", registry_hash="a" * 64,
                dependency_checks_passed=True)

        def rank_legal_routes(self, *, runtime_identity, task_context, workflow_id,
                              candidate_routes, expected_workflow_revision):
            return WorkflowRouteRanking(
                legal_routes=(), scores={}, workflow_revision=expected_workflow_revision,
                route_contract_digests={}, registry_hash="a" * 64,
                workflow_id=workflow_id)

        def validate_route_ranking(self, *, runtime_identity, task_context, ranking):
            return True

    port = validate_workflow_port(HostWorkflows())
    assert isinstance(port, WorkflowPort)
    assert port.snapshot(runtime_identity={}, task_context={}) == {"revision": 1}
    assert port.active_guidance(runtime_identity={}, task_context={})["guidance"] == []
    assert port.apply_step(runtime_identity={}, step={"step": "advance"})["accepted"]
    assert port.rank_legal_routes(
        runtime_identity={}, task_context={}, workflow_id="example",
        candidate_routes=(), expected_workflow_revision=0).legal_routes == ()

    with pytest.raises(TypeError, match="snapshot.*rank_legal_routes"):
        validate_workflow_port(object())


def test_resolver_uses_host_workflow_capability_without_provider_loading():
    resolve = _resolver()
    subject = "database:service-a"
    workflow = {
        "id": "service-workflow", "version": "1.0.0",
        "manifest_hash": "manifest-hash", "runtime_hash": "runtime-hash",
        "domain": "database", "purpose": "host integration fixture",
        "selected_flow": None, "selected_specialists": (),
        "workflow_engine_kind": "host", "workflow_contract_version": "1.0.0",
        "workflow_contract_hash": "workflow-contract-hash", "flow_selection": {},
        "subject_ref": subject, "composition": {}, "inputs": {},
        "emits": [], "integrations": {}, "flow_integrations": {},
    }

    class HostWorkflowPort:
        def __init__(self):
            self.resolved_with = None

        def snapshot(self, *, runtime_identity, task_context):
            return {"revision": 1}

        def active_guidance(self, *, runtime_identity, task_context):
            return {"revision": 1, "guidance": []}

        def apply_step(self, *, runtime_identity, step):
            return {"accepted": True}

        def eligible_routes(self, *, runtime_identity, task_context):
            assert runtime_identity["subject_ref"] == subject
            assert task_context["domains"] == ("database",)
            return WorkflowEligibility(
                eligible_routes=({"route_type": "statework", "route": "service-workflow",
                                  "statework_id": "service-workflow"},),
                workflow_versions={"service-workflow": "1.0.0"})

        def resolve_workflows(self, *, runtime_identity, task_context, routing_adjustments):
            self.resolved_with = tuple(routing_adjustments)
            assert runtime_identity["task_id"] == "host-workflow-task"
            return WorkflowResolution(
                execution_order=(workflow,), active_workflows=(workflow,), capsules={},
                component_requirements=(), component_enhancements=(),
                registry_version="2.0.0", registry_hash="a" * 64,
                dependency_checks_passed=True)

        def rank_legal_routes(self, *, runtime_identity, task_context, workflow_id,
                              candidate_routes, expected_workflow_revision):
            return WorkflowRouteRanking(
                legal_routes=(), scores={}, workflow_revision=expected_workflow_revision,
                route_contract_digests={}, registry_hash="a" * 64,
                workflow_id=workflow_id)

        def validate_route_ranking(self, *, runtime_identity, task_context, ranking):
            return True

    port = HostWorkflowPort()
    request = resolve.TaskRequest(
        task_id="host-workflow-task", application_id="app", shape="quick",
        domain_tags=("database",), subject_ref=subject)
    from runtime_host.host_api import CognitiveRuntime
    task = CognitiveRuntime(telemetry_disabled=True, workflow_port=port).begin_task(
        request, runtime_mode="standalone_advisory")

    assert port.resolved_with == ()
    assert port.eligible_routes
    task.close()


def test_resolver_does_not_own_csw_loading_primitives():
    source = (SCRIPTS / "resolve-runtime.py").read_text(encoding="utf-8")
    assert "_load_compiled_csw_registry" not in source
    assert "_CSW_CONTROL_MODULES" not in source
    assert "_CSW_COMPILED_REGISTRIES" not in source
    assert "control.FlowSelector" not in source
    assert "module.PacketStore" not in source
    task_source = (SCRIPTS / "resolver" / "task_resolution.py").read_text(encoding="utf-8")
    assert "resolver.workflows" not in task_source
    assert "plan_statework_dag" not in task_source
    assert "match_stateworks" not in task_source


def test_cfw_runtime_uses_the_neutral_state_authority_port():
    source = (SCRIPTS / "runtime.py").read_text(encoding="utf-8")
    assert "class CSWStateAuthority" not in source
    assert "import cognitive_statework" not in source
    assert "from runtime_session.host_authority import" in source
    assert "create_state_authority" in source
    resolver_source = (SCRIPTS / "resolve-runtime.py").read_text(encoding="utf-8")
    assert "COW_ROOT" not in resolver_source
    assert "cow_runtime_hash" not in resolver_source
    assert "runtime_hash_paths" not in resolver_source
    assert "behavior_hash_paths" not in resolver_source


def test_partial_component_lease_expiry_preserves_other_live_targets():
    from types import SimpleNamespace
    from runtime import LifecycleController
    from runtime_session.legacy_external_behavior import (
        component_overlay_from_legacy_behavior_state)

    scope = "dp-runtime-" + "a" * 64
    state = {
        "revision": 3,
        "cognitive_mode": "RECOVER",
        "framework_modes": {name: ("mandatory" if name == "ward" else "normal")
                            for name in COMPONENT_IDS},
        "active_interventions": [
            {
                "intervention_id": "owl-lease", "start_revision": 2,
                "scope": scope, "baseline_action_index": 0,
                "expires_after_actions": 1,
                "expires_at_action": 0,
                "targets": [{"target": "framework:owl",
                             "previous_value": "normal", "applied_value": "off"}],
            },
            {
                "intervention_id": "flow-lease", "start_revision": 3,
                "scope": scope, "baseline_action_index": 0,
                "expires_after_actions": 3,
                "expires_at_action": 2,
                "targets": [{"target": "framework:flow",
                             "previous_value": "normal", "applied_value": "off"}],
            },
        ],
    }
    overlay = component_overlay_from_legacy_behavior_state(state)
    lifecycle = LifecycleController(SimpleNamespace(kernel_instructions=()))
    prepared = lifecycle.prepare_component_overlay(overlay)
    lifecycle.apply_component_overlay(prepared, startup_active=set())

    assert lifecycle.component_mode("owl") == "off"
    assert lifecycle.component_mode("flow") == "off"
    assert lifecycle.expire_behavior_leases(1) is True
    assert lifecycle.component_mode("owl") == "normal"
    assert lifecycle.component_mode("flow") == "off"
    assert lifecycle.component_mode("ward") == "mandatory"
    assert lifecycle.expire_behavior_leases(1) is False
    assert lifecycle.expire_behavior_leases(3) is True
    assert lifecycle.component_mode("flow") == "normal"


def test_latest_active_layer_wins_then_restores_oldest_previous_target():
    from runtime_contract.component_control import AppliedControlLease
    from runtime_contract.component_control import ComponentControl

    controls = tuple(ComponentControl(
        component_id=name,
        mode="mandatory" if name == "ward" else "normal",
        scope=None, expires_at_action=None, source_revision=3,
    ) for name in COMPONENT_IDS)
    overlay = ComponentOverlay(
        behavior_revision=3, behavior_mode="RECOVER", controls=controls,
        target_leases=(
            AppliedControlLease("d1", "i1", "framework:flow", "off", "normal", 2, 1),
            AppliedControlLease("d2", "i2", "framework:flow", "elevated", "off", 0, 2),
        ),
    )

    assert overlay.effective_target_value("framework:flow", 0) == "elevated"
    assert overlay.effective_target_value("framework:flow", 1) == "off"
    assert overlay.effective_target_value("framework:flow", 3) == "normal"


def test_explicit_ineligible_flow_is_rejected():
    resolve = _resolver()
    cow = COW
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
        def advice_for_task(self, *, static_policy, **kwargs):
            seen.update({**kwargs, "static_policy": static_policy})
            return {**static_policy, "status": "ok", "adjustments": []}
    cow = COW
    request = resolve.TaskRequest(task_id="flow-advice", application_id="app",
                                  shape="quick", domain_tags=("infrastructure",),
                                  operation="deploy", subject_ref="repo")
    resolve.resolve(request, cow_root=cow, behavioral_advisor=Advisor())
    routes = seen["eligible_routes"]
    assert any(item["route_type"] == "statework" and item["route"] == "infrae" for item in routes)
    assert any(item["route_type"] == "flow" and item["statework_id"] == "infrae" for item in routes)

def test_legal_flow_preference_changes_selection():
    resolve = _resolver()
    cow = COW
    class Advisor:
        def __init__(self, route): self.route = route
        def advice_for_task(self, *, static_policy, **kwargs):
            return {**static_policy, "status": "ok", "adjustments": [{
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
    cow = COW
    infrae = next(item for item in resolve.load_stateworks(cow) if item["id"] == "infrae")
    routes = resolve.enumerate_eligible_routes(infrae, cow, operation="deploy", task_shape="implement")
    assert "change" in {route["route"] for route in routes if route["route_type"] == "flow"}
    assert all(route["route"] != "debug" for route in routes if route["route_type"] == "flow")


def test_flow_activation_uses_bounded_semantic_change_impact():
    resolve = _resolver()
    from cognitive_runtime import CognitiveRuntime

    runtime = CognitiveRuntime(telemetry_disabled=True)
    task = runtime.begin_task(resolve.TaskRequest(
        task_id="flow-impact", application_id="flow-impact-test",
        shape="performance", subject_ref="repo:flow-impact"),
        telemetry_disabled=True)
    host = runtime.host_handle(task)
    session = host._session()
    try:
        host.on_artifact(impact_facts={
            "shared_contract": True, "modified_owner_count": 1,
            "fanout": 1, "change_amplification_score": 0.2,
        })
        assert "flow" not in session.lifecycle_workflow.lifecycle.artifact_active

        host.on_artifact(impact_facts={
            "cross_boundary": True, "shared_contract": True,
            "modified_owner_count": 3, "fanout": 4,
            "change_amplification_score": 0.65,
        })
        assert "flow" in session.lifecycle_workflow.lifecycle.artifact_active

        with pytest.raises(ValueError, match="fanout"):
            host.on_artifact(impact_facts={"fanout": 257})
    finally:
        task.close()


def test_cfw_runtime_core_does_not_embed_harness_protocol_names():
    # Harness-specific naming belongs in runtime-core integrations, not in
    # reusable CFW runtime contracts/session substrate.
    core_dirs = (ROOT / "scripts" / "runtime_contract",
                 ROOT / "scripts" / "runtime_session",
                 ROOT / "scripts" / "runtime_host")
    forbidden_tokens = (
        "codex_host", "thread/start", "turn/start", "item/tool/call",
        "CODEX_HOME", "Claude", "Athena",
    )
    violations = []
    for core_dir in core_dirs:
        for path in sorted(core_dir.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token.casefold() in source.casefold():
                    violations.append(
                        f"{path.relative_to(ROOT)} contains {token!r}")
    assert not violations, "CFW runtime core contains harness names:\n" + "\n".join(
        violations)


def test_codex_wire_methods_are_owned_by_runtime_core():
    codex_dir = ROOT / "scripts" / "codex_host"
    compatibility_modules = tuple(sorted(codex_dir.glob("*.py")))
    protocol_names = (
        "CODEX_HOME", "thread/start", "thread/resume", "turn/start",
        "turn/interrupt", "item/tool/call", "turn/completed",
    )
    violations = [
        f"{path.relative_to(ROOT)} contains {token!r}"
        for path in compatibility_modules
        for token in protocol_names
        if token in path.read_text(encoding="utf-8")
    ]
    assert not violations, "Codex wire details remain in CFW compatibility modules:\n" + "\n".join(
        violations)
    from cognitive_runtime_core.integrations.codex.protocol import CodexWireMethod
    from cognitive_runtime_core.integrations.codex.cfw_host.actions import (
        CodexActionBinding as RuntimeCoreActionBinding,
    )
    from cognitive_runtime_core.integrations.codex.cfw_host.adapter import (
        CodexAppServerAdapter as RuntimeCoreAdapter,
    )
    from codex_host.actions import CodexActionBinding
    from codex_host.adapter import CodexAppServerAdapter

    assert CodexWireMethod.THREAD_START == "thread/start"
    assert CodexWireMethod.TURN_START == "turn/start"
    assert CodexActionBinding is RuntimeCoreActionBinding
    assert CodexAppServerAdapter is RuntimeCoreAdapter
    assert CodexAppServerAdapter.__module__ == (
        "cognitive_runtime_core.integrations.codex.cfw_host.adapter")
