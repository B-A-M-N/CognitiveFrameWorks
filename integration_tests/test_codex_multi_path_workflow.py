"""Host-owned multi-StateWork selection through the generic Codex runtime."""
from __future__ import annotations

import sys
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import asyncio
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CSW_ROOT = ROOT.parent / "CognitiveStateWork"
sys.path.insert(0, str(ROOT / "scripts"))

import resolve_runtime  # noqa: E402
from runtime_contract.workflow_port import ProviderWorkflowPlanner  # noqa: E402
from codex_app_server_adapter import (  # noqa: E402
    CodexActionBinding, CodexAppServerAdapter,
)
from digital_psychology_adapter import CFWBehaviorAdapter  # noqa: E402
from cognitive_runtime_core.integrations.cfw import (  # noqa: E402
    DigitalPsychologyHarnessLifecycle,
)
from runtime_contract.actions import default_action_registry  # noqa: E402
from runtime_contract.task import (  # noqa: E402
    AcceptanceCriterion, TaskGoalContract,
)
from runtime_host.host_api import CognitiveRuntime  # noqa: E402

_CSW_PACKAGE = "cognitive_statework"
if _CSW_PACKAGE not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _CSW_PACKAGE, CSW_ROOT / "scripts" / "__init__.py",
        submodule_search_locations=[str(CSW_ROOT / "scripts")])
    if _spec is None or _spec.loader is None:
        raise ImportError("could not load the local CSW provider package")
    _package = importlib.util.module_from_spec(_spec)
    sys.modules[_CSW_PACKAGE] = _package
    _spec.loader.exec_module(_package)

from cognitive_statework.cfw_provider import CognitiveStateWorkProvider  # noqa: E402


class _SharedCSWWorkflowPort:
    """Expose one CSW authority through CFW's neutral workflow port."""

    def __init__(self, provider, state_authority, authority):
        self.authority = authority
        self.planner = ProviderWorkflowPlanner(
            provider, state_authority=state_authority)

    @staticmethod
    def _scope(identity):
        return {key: str(identity[key]) for key in (
            "namespace_id", "application_id", "application_instance_id",
            "workflow_id", "subject_ref")}

    def snapshot(self, *, runtime_identity, task_context):
        return self.authority.behavioral_snapshot(
            workflow_scope=self._scope(runtime_identity),
            candidate_statework_ids=task_context.get("candidate_statework_ids"))

    def active_guidance(self, *, runtime_identity, task_context):
        snapshot = self.snapshot(runtime_identity=runtime_identity,
                                 task_context=task_context)
        return {"revision": int(snapshot.get("revision", 0)), "guidance": []}

    def apply_step(self, *, runtime_identity, step):
        statework_id = str(step["statework_id"])
        contract = self.authority.workflow_contract_identity(statework_id)
        identity = {**self._scope(runtime_identity),
                    "statework_id": statework_id,
                    "contract_version": contract["contract_version"]}
        result = self.authority.apply_step(
            identity, contract_hash=contract["contract_hash"],
            step_id=str(step["step_id"]), evidence_refs=step.get("evidence_refs", ()),
            expected_revision=int(step["expected_revision"]))
        return {"accepted": bool(result.allowed), "reason": str(result.reason)}

    def eligible_routes(self, **kwargs):
        return self.planner.eligible_routes(**kwargs)

    def resolve_workflows(self, **kwargs):
        return self.planner.resolve_workflows(**kwargs)

    def rank_legal_routes(self, **kwargs):
        return self.planner.rank_legal_routes(**kwargs)

    def validate_route_ranking(self, **kwargs):
        return self.planner.validate_route_ranking(**kwargs)


class _AppServerClient:
    """Minimal generic App Server transport for host-startup integration."""

    host_policy_verified = True

    def __init__(self):
        self.codex_feature_catalog = {
            "codex_cli_version": "codex-cli 0.160.0",
            "feature_names": ["dynamic_tools"],
        }
        self.handlers = None
        self.thread_params = None
        self.closed = False

    def set_handlers(self, *, request_handler, notification_handler):
        self.handlers = (request_handler, notification_handler)

    async def request(self, method, params=None, *, timeout=30.0):
        if method == "initialize":
            return {}
        if method == "account/read":
            return {"account": {"type": "apiKey"},
                    "requiresOpenaiAuth": False}
        if method == "configRequirements/read":
            return {"requirements": None}
        if method == "thread/start":
            self.thread_params = dict(params or {})
            return {"thread": {"id": "generic-thread-1",
                               "sessionId": "generic-appserver-session-1"}}
        raise AssertionError(f"unexpected App Server request: {method}")

    async def notify(self, method, params=None):
        return None

    async def close(self):
        self.closed = True


def test_host_task_can_select_getter_and_resolve_its_gitter_dependency():
    subject_ref = "repo:codex-multi-path"
    request = resolve_runtime.TaskRequest(
        task_id="codex-multi-path",
        application_id="generic-codex-appserver",
        subject_ref=subject_ref,
        shape="implement",
        domain_tags=("repository_state",),
        requested_statework_ids=("getter",),
        statework_subject_refs={"getter": subject_ref},
    )

    provider = CognitiveStateWorkProvider(CSW_ROOT)
    bundle = resolve_runtime.resolve(
        request, cow_root=CSW_ROOT,
        workflow_port=ProviderWorkflowPlanner(provider))
    selected = [str(item["id"]) for item in bundle.stateworks]

    assert selected == ["gitter", "getter"]
    assert bundle.workflow_task_context["requested_statework_ids"] == ("getter",)
    assert bundle.statework_root == str(CSW_ROOT.resolve())


def test_requested_statework_ids_are_bounded_and_canonicalized():
    request = resolve_runtime.TaskRequest(
        task_id="codex-multi-path-order",
        application_id="generic-codex-appserver",
        requested_statework_ids=("getter", "gitter"),
    )

    assert request.requested_statework_ids == ("getter", "gitter")

    try:
        resolve_runtime.TaskRequest(
            task_id="codex-multi-path-duplicate",
            application_id="generic-codex-appserver",
            requested_statework_ids=("gitter", "gitter"),
        )
    except ValueError as exc:
        assert "unique bounded IDs" in str(exc)
    else:
        raise AssertionError("duplicate requested StateWorks must be rejected")


def test_generic_codex_opens_cfw_and_dp_on_the_same_multi_workflow_authority(tmp_path):
    """A host-selected Getter plan reaches the private DP bridge intact."""
    if str(CSW_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(CSW_ROOT / "scripts"))
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    DP_ROOT = ROOT.parent / "DigitalPsychology"
    if str(DP_ROOT) not in sys.path:
        sys.path.insert(0, str(DP_ROOT))

    from cognitive_statework.control_plane import (  # noqa: PLC0415
        EvidenceStore, SubjectStateStore, WorkflowAuthority,
        WorkflowSelectionStore,
    )
    from lib.behavior_state import GoalItemSnapshot, GoalSetSnapshot  # noqa: PLC0415
    from lib.control.service import ControlPlaneService  # noqa: PLC0415
    from lib.host import InProcessControlClient  # noqa: PLC0415
    from lib.runtime.adapters.cfw import CFWRuntimeAdapter  # noqa: PLC0415
    from lib.runtime.adapters.csw import (  # noqa: PLC0415
        CSWRuntimeAdapter, CSWWorkflowBinding,
    )
    from lib.runtime.contracts import (  # noqa: PLC0415
        RuntimeIdentity,
    )
    from lib.runtime.host import RuntimeHost  # noqa: PLC0415
    from lib.runtime.store import BehaviorStateStore  # noqa: PLC0415
    from lib.service import DigitalPsychologyService  # noqa: PLC0415
    from lib.system_one_backends import DisabledSystemOneBackend  # noqa: PLC0415
    from lib.system_one_service import SystemOneService  # noqa: PLC0415

    task_id = "codex-shared-multipath"
    subject_ref = f"repo:{task_id}"
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "parser.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    git_name = subprocess.check_output(
        ["git", "config", "--global", "--get", "user.name"], text=True).strip()
    git_email = subprocess.check_output(
        ["git", "config", "--global", "--get", "user.email"], text=True).strip()
    subprocess.run(["git", "config", "user.name", git_name],
                   cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", git_email],
                   cwd=workspace, check=True)
    subprocess.run(["git", "add", "src/parser.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "seed repository"],
                   cwd=workspace, check=True)
    request = resolve_runtime.TaskRequest(
        task_id=task_id, namespace_id="codex-test",
        application_id="generic-codex-appserver",
        application_instance_id="generic-codex-appserver-test",
        subject_ref=subject_ref, shape="implement",
        domain_tags=("repository_state",), operation="repair",
        requested_statework_ids=("getter",),
        statework_subject_refs={"getter": subject_ref},
        goal_contract=TaskGoalContract(
            objective="Verify a host-selected multi-workflow plan",
            deliverable_kind="code_behavior", work_mode="delivery",
            task_complexity="complex",
            acceptance_criteria=(AcceptanceCriterion(
                "shared_authority", "CFW and DP use the same CSW authority",
                "host_evidence"),)))

    state_root = tmp_path / "csw-state"
    state_root.mkdir(mode=0o700)
    state_path = state_root / "subjects.sqlite3"
    selection_path = state_root / "selections.sqlite3"
    evidence_path = state_root / "evidence.json"
    state_store = SubjectStateStore(state_path)
    evidence_store = EvidenceStore()
    evidence_store.save(evidence_path)
    authority = WorkflowAuthority(
        root=CSW_ROOT, state_store=state_store, evidence_store=evidence_store,
        selection_store=WorkflowSelectionStore(selection_path),
        storage_identity={"state_store": str(state_path.resolve()),
                          "selection_store": str(selection_path.resolve()),
                          "evidence_store": str(evidence_path.resolve())})
    provider = CognitiveStateWorkProvider(CSW_ROOT)
    state_authority = provider.create_state_authority(
        state_path=state_path, evidence_path=evidence_path,
        namespace_id=request.namespace_id, application_id=request.application_id,
        application_instance_id=request.application_instance_id,
        workflow_id=request.task_id, workflow_authority=authority)
    workflow_port = _SharedCSWWorkflowPort(provider, state_authority, authority)
    runtime = CognitiveRuntime(telemetry_disabled=True, workflow_port=workflow_port)

    dp_state = tmp_path / "dp-private"
    dp_state.mkdir(mode=0o700)
    decision_service = DigitalPsychologyService(
        event_db=dp_state / "events.sqlite3",
        system_one_service=SystemOneService(
            DisabledSystemOneBackend(), mode="disabled"))
    dp_control = ControlPlaneService(
        decision_service=decision_service,
        state_store=BehaviorStateStore(dp_state / "behavior.sqlite3"))
    dp_client = InProcessControlClient(dp_control, close_service=True)
    dp_host = RuntimeHost(model_visible_tools=("cfw_read",))
    dp_bindings = []
    prepared_workflows = {}

    async def bridge_factory(*, request, task_handle, host_handle, session_id,
                             **_kwargs):
        behavior = task_handle.create_component_adapter(CFWBehaviorAdapter)
        cfw_workflows = behavior.workflow_authority_identities()
        assert sorted(cfw_workflows) == ["getter", "gitter"]
        first_identity = cfw_workflows[sorted(cfw_workflows)[0]]
        runtime_identity = RuntimeIdentity(
            namespace_id=first_identity["namespace_id"],
            application_id=first_identity["application_id"],
            application_instance_id=first_identity["application_instance_id"],
            agent_id=request.agent_id,
            agent_instance_id=task_handle.agent_instance_id,
            session_id=session_id, task_id=request.task_id,
            attempt_id=task_handle.attempt_id,
            workflow_id=first_identity["workflow_id"],
            subject_ref=first_identity["subject_ref"],
            segment_id="segment-0",
            behavioral_subject=first_identity["subject_ref"],
            goal_contract_hash=str(task_handle.bundle.pinned.get(
                "goal_contract_hash") or "") or None)
        bindings = {}
        for statework_id, cfw_identity in cfw_workflows.items():
            contract = authority.workflow_contract_identity(statework_id)
            bindings[statework_id] = CSWWorkflowBinding(
                identity={key: cfw_identity[key] for key in (
                    "namespace_id", "application_id", "application_instance_id",
                    "workflow_id", "subject_ref")}
                    | {"statework_id": statework_id,
                       "contract_version": contract["contract_version"]},
                contract_hash=contract["contract_hash"])
        statework_workflows = {
            statework_id: {**dict(identity), "statework_id": statework_id}
            for statework_id, identity in cfw_workflows.items()}
        gitter_result = _complete_gitter_workflow(
            authority, statework_workflows["gitter"], workspace,
            request, host_handle)
        prepared_workflows["gitter"] = gitter_result
        prepared_workflows["getter"] = _prepare_getter_workflow(
            authority, statework_workflows["getter"], workspace,
            request, host_handle,
            gitter_result=gitter_result)
        csw_port = CSWRuntimeAdapter(
            authority, identity=runtime_identity, bindings=bindings)
        dp_bindings.append(csw_port)
        bridge = await dp_host.open_control_bridge(
            identity=runtime_identity,
            objective=request.goal_contract.objective,
            objective_criteria=("shared_authority",),
            initial_goal_set=GoalSetSnapshot(revision=0, items=(
                GoalItemSnapshot(criterion_id="shared_authority", status="READY"),)),
            cfw=CFWRuntimeAdapter(behavior), csw=csw_port, client=dp_client,
            csw_authority_identity=csw_port.authority_identity())
        return bridge

    def unexpected_candidate_factory(**_kwargs):
        raise AssertionError("this startup-only integration test does not invoke tools")

    def unexpected_observation_factory(**_kwargs):
        raise AssertionError("this startup-only integration test does not invoke tools")

    runtime_lifecycle = DigitalPsychologyHarnessLifecycle(
        bridge_factory=bridge_factory,
        component_adapter_factory=CFWBehaviorAdapter,
        candidate_factory=unexpected_candidate_factory,
        observation_factory=unexpected_observation_factory)
    app_server = _AppServerClient()
    read_binding = CodexActionBinding(
        name="cfw_read", description="Read a host-authorized repository file.",
        input_schema={"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"],
            "additionalProperties": False},
        action_binding_id="read", descriptor=default_action_registry()["read"],
        executor=lambda _args: {"content": "test"})
    adapter = CodexAppServerAdapter(
        request=request, action_bindings=(read_binding,), model="test-model", cwd=tmp_path,
        runtime=runtime, runtime_harness_lifecycle=runtime_lifecycle,
        runtime_mode="combined_enforcing",
        task_start_options={"state_authority": state_authority})

    if str(ROOT / "integration_tests") not in sys.path:
        sys.path.insert(0, str(ROOT / "integration_tests"))
    from codex_eval.csw_setup import (  # noqa: PLC0415
        _complete_gitter_workflow, _prepare_getter_workflow,
    )

    async def run():
        await adapter.open(app_server)
        try:
            cfw_workflows = adapter._task_handle.create_component_adapter(
                CFWBehaviorAdapter).workflow_authority_identities()
            statework_workflows = {
                statework_id: {**dict(identity), "statework_id": statework_id}
                for statework_id, identity in cfw_workflows.items()}
            gitter_result = prepared_workflows["gitter"]
            getter_prepared = prepared_workflows["getter"]
            getter_contract_hash = str(
                authority.workflow_contract_identity("getter")["contract_hash"])
            getter_state = authority.inspect_workflow(
                statework_workflows["getter"],
                contract_hash=getter_contract_hash)
            assert gitter_result["repository_truth_valid"] is True
            assert getter_prepared["started"] is True
            assert getter_state.current_state == "CHANGING"
            assert getter_state.revision == 3
            assert all(authority.evidence_store.resolve([ref])
                       for ref in getter_prepared["evidence_refs"].values())
            cfw_workflows = adapter._task_handle.create_component_adapter(
                CFWBehaviorAdapter).workflow_authority_identities()
            dp_workflows = dp_bindings[0].workflow_authority_identities()
            assert sorted(cfw_workflows) == ["getter", "gitter"]
            assert {key: dict(value) for key, value in cfw_workflows.items()} == {
                key: dict(value) for key, value in dp_workflows.items()}
            status = adapter.status()
            assert status["model_visible_dp_tools"] == 0
            assert status["runtime_mode"] == "combined_enforcing"
            assert status["cognitive_fault"] is None
            assert status["runtime_harness_lifecycle_status"][
                "private_dp_bridge_open"] is True
            assert status["runtime_harness_lifecycle_status"][
                "shared_csw_authority_verified"] is True
            assert status["runtime_harness_lifecycle_status"][
                "shared_csw_workflow_ids"] == ["getter", "gitter"]
            assert status["runtime_harness_lifecycle_status"]["dp_faulted"] is False
            assert status["runtime_harness_lifecycle_status"]["production_metrics"][
                "counters"]["csw_directive_rejections"] == 0
            visible_tools = app_server.thread_params["dynamicTools"]
            assert [item["name"] for item in visible_tools] == ["cfw_read"]
            assert all(item["name"].startswith("cfw_")
                       for item in visible_tools)
        finally:
            await adapter.close()
            dp_host.close()

    asyncio.run(run())
