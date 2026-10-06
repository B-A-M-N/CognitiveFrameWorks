#!/usr/bin/env python3
"""Build host-owned runtime treatments for real-agent cohort trials.

The benchmark harness speaks JSON to an agent subprocess. On its own that
measures the model, not the system under test: it never builds CFW, never binds
DP, and therefore cannot observe starvation, mode behavior, or fairness. This
module supplies that missing half.

The construction helpers below are also used by the per-trial host adapter. A
qualification trial must keep one host/runtime instance alive across model
turns, inject context from that instance, and execute model-proposed actions at
the host boundary. Merely calling :func:`build_session` is a construction
probe, not trial exposure.

The five configurations are:

- ``baseline``       : no CFW, CSW, DP, or Laya runtime.
- ``cfw``            : CFW only.
- ``cfw_csw``        : CFW plus one shared CSW WorkflowAuthority.
- ``cfw_csw_dp``     : the above plus DP through its private stdio MCP.
- ``cfw_csw_dp_laya``: the above plus a live Laya attestation from that MCP.

Nothing here is stubbed. If a configuration cannot be constructed, that is
reported rather than silently downgraded to a weaker cell.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
COW = Path(os.environ.get("COGNITIVE_STATEWORK_ROOT",
                          ROOT.parent / "CognitiveStateWork")).resolve()
DP = Path(os.environ.get("DIGITALPSYCHOLOGY_ROOT",
                         ROOT.parent / "DigitalPsychology")).resolve()

# Required provider attestation for a real semantic treatment (P1-16).
REQUIRED_PROVIDER_ATTESTATION = (
    "backend_id", "model_id", "model_revision", "model_content_hash",
    "provider_protocol_hash", "calibration_bundle_hash", "provider_mode",
    "capability_digest", "enabled_question_families",
)


class HarnessError(RuntimeError):
    """The requested configuration could not be constructed as specified."""


class InvalidTreatment(HarnessError):
    """A treatment arm ran without the capability it claims."""


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _cfw_modules():
    """Load CFW's resolver and runtime from the current checkout."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    resolve = _load("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
    runtime = _load("cfw_runtime", ROOT / "scripts" / "runtime.py")
    return resolve, runtime


def _real_csw_authority(state_root: Path):
    if str(COW / "scripts") not in sys.path:
        sys.path.insert(0, str(COW / "scripts"))
    from control_plane import (EvidenceStore, SubjectStateStore,
                               WorkflowAuthority, WorkflowSelectionStore)
    subject_store = SubjectStateStore(state_root / "csw-subjects.json")
    evidence_store = EvidenceStore()
    evidence_path = state_root / "csw-evidence.json"
    evidence_store.save(evidence_path)
    selection_store = WorkflowSelectionStore(state_root / "csw-selections.json")
    return WorkflowAuthority(
        root=COW, state_store=subject_store, evidence_store=evidence_store,
        selection_store=selection_store,
        storage_identity={
            "state_store": str(Path(subject_store.path).resolve()),
            "selection_store": str(Path(selection_store.path).resolve()),
            "evidence_store": str(evidence_path.resolve()),
        })


DP_CONFIGURATIONS = frozenset({"cfw_csw_dp", "cfw_csw_dp_laya"})
CSW_CONFIGURATIONS = frozenset({"cfw_csw", *DP_CONFIGURATIONS})
FRAMEWORK_CONFIGURATIONS = frozenset({"cfw", *CSW_CONFIGURATIONS})
AGENT_FILE_TOOLS = ("read", "inspect", "write")
MAX_HOST_ACTIONS_PER_TURN = 8
MAX_HOST_ACTIONS_PER_TRIAL = 128
MAX_READ_BYTES = 32_768


def _runtime_identity(task_id: str, criterion_id: str):
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from lib.runtime.contracts import RuntimeIdentity
    return RuntimeIdentity(
        namespace_id="tenant-cohort", application_id="cohort-app",
        application_instance_id="cohort-instance", agent_id="codex",
        agent_instance_id=f"codex-cohort-{task_id}", session_id=f"session-{task_id}",
        task_id=task_id, attempt_id="attempt-1",
        workflow_id=("private:tenant-cohort:cohort-instance:"
                     f"codex-cohort-{task_id}"), subject_ref=f"repo:{task_id}",
        segment_id="segment-0", behavioral_subject=f"repo:{task_id}")


def _csw_snapshot_context(authority: Any, identity: Any,
                          candidate_ids: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    if authority is None:
        return "", {}
    raw = authority.behavioral_snapshot(
        workflow_scope=identity.csw_scope(), candidate_statework_ids=candidate_ids)
    # Emit the authority's bounded current view, not model-supplied workflow data.
    active = [{"statework_id": row["identity"]["statework_id"],
               "state": row["current_state"], "revision": row["revision"],
               "eligible_steps": [step["step_id"] for step in row.get("eligible_steps", ())]}
              for row in raw.get("active_workflows", ())]
    eligible = [str(row["statework_id"])
                for row in raw.get("eligible_stateworks", ())]
    snapshot = {"authority_id": str(getattr(authority, "authority_instance_id", "")),
                "registry_hash": str(raw.get("registry_hash") or ""),
                "snapshot_id": str(raw.get("snapshot_id") or ""),
                "selection_revision": int(raw.get("selection_revision", 0)),
                "active_workflows": active, "eligible_stateworks": eligible}
    text = ("===== CSW AUTHORITATIVE WORKFLOW CONTEXT =====\n" +
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
    return text, snapshot


def _safe_workspace_target(workspace: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("host file action requires a non-empty target")
    target = Path(raw)
    if not target.is_absolute():
        target = workspace / target
    target = target.resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError as exc:
        raise PermissionError("host file action escaped the trial workspace") from exc
    return target


def execute_host_file_action(action: Mapping[str, Any], *, workspace: Path) -> dict[str, Any]:
    """Execute an allowlisted filesystem action as the trusted trial host."""
    binding = action.get("tool_binding_id") or action.get("tool")
    args = action.get("arguments") or {}
    if not isinstance(args, Mapping):
        raise ValueError("host file action arguments must be an object")
    target = _safe_workspace_target(workspace, args.get("target", args.get("path")))
    operation = args.get("operation")
    if binding in {"read", "inspect"}:
        if operation == "list":
            if target.is_file():
                entries = [target.name]
            else:
                entries = sorted(path.name for path in target.iterdir())[:256]
            return {"status": "pass", "operation": "list", "target": str(target),
                    "entries": entries}
        data = target.read_bytes()
        if len(data) > MAX_READ_BYTES:
            data = data[:MAX_READ_BYTES]
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            content = data.decode("utf-8", errors="replace")
        return {"status": "pass", "operation": "read", "target": str(target),
                "content": content,
                "content_digest": hashlib.sha256(data).hexdigest(),
                "truncated": target.stat().st_size > len(data)}
    if binding != "write":
        raise PermissionError(f"host tool {binding!r} is not registered")
    content = args.get("content")
    if not isinstance(content, str):
        raise ValueError("host write content must be text")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.cohort-partial")
    try:
        staging.write_text(content, encoding="utf-8")
        os.replace(staging, target)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"status": "pass", "operation": "write", "target": str(target),
            "content_digest": digest, "bytes_written": target.stat().st_size}


class RecordingCFWPort:
    """Transparent CFW port that preserves gate evidence for the host receipt."""
    def __init__(self, component_port: Any) -> None:
        self.component_port = component_port
        self.gate_receipts: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.component_port, name)

    def preflight_action(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self.component_port.preflight_action(action)
        self.gate_receipts.append({"stage": "preflight", **dict(result)})
        return result

    def authorize_action(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self.component_port.authorize_action(action)
        self.gate_receipts.append({"stage": "final", **dict(result)})
        return result


class CohortTrialHost:
    """One real treatment instance that owns context, actions and receipts."""

    def __init__(self, *, configuration: str, task: Mapping[str, Any],
                 state_root: Path, workspace: Path) -> None:
        self.configuration = configuration
        self.task = dict(task)
        self.task_id = str(task.get("task_id") or
                           f"{task.get('task_family', 'trial')}-{task.get('trial', 0)}")
        self.criterion_id = f"{self.task_id}-criterion"
        self.state_root = state_root
        self.workspace = workspace.resolve()
        self.authority = None
        self.cfw_session = None
        self.bridge = None
        self.client = None
        self.dp_identity = None
        self.csw_candidate_ids: tuple[str, ...] = ()
        self.cfw_port = None
        self.receipts: list[dict[str, Any]] = []
        self.context_hashes: list[str] = []
        self.action_count = 0
        self.exposure: dict[str, Any] = {}
        self.closed = False

    async def open(self) -> "CohortTrialHost":
        if self.configuration not in {"baseline", "cfw", "cfw_csw", *DP_CONFIGURATIONS}:
            raise HarnessError(f"unknown cohort configuration {self.configuration!r}")
        self.state_root.mkdir(parents=True, exist_ok=True)
        use_csw = self.configuration in CSW_CONFIGURATIONS
        use_cfw = self.configuration in FRAMEWORK_CONFIGURATIONS
        if use_csw:
            self.authority = _real_csw_authority(self.state_root)
        if use_cfw:
            resolve, runtime = _cfw_modules()
            authority_options = {}
            if self.authority is not None:
                evidence_path = Path(self.authority.storage_components["evidence_store"])
                authority_options["state_authority"] = runtime.create_state_authority(
                    self.authority.root,
                    state_path=self.authority.state_store.path,
                    evidence_path=evidence_path,
                    workflow_authority=self.authority)
            protected = runtime.HostExecutionContext
            from runtime_contract.actions import ProtectedResourcePolicy
            default_policy = ProtectedResourcePolicy.default()
            workspace_root = str(self.workspace)
            policy = ProtectedResourcePolicy(
                protected_roots=default_policy.protected_roots,
                authorized_write_roots=(workspace_root,))
            host_context = protected(
                principal="runtime", task_scope=self.task_id,
                filesystem_scope=(workspace_root,), task_id=self.task_id,
                protected_resources=policy)
            request = resolve.TaskRequest(
                task_id=self.task_id, namespace_id="tenant-cohort",
                application_id="cohort-app", application_instance_id="cohort-instance",
                agent_id="codex", agent_instance_id=f"codex-cohort-{self.task_id}",
                subject_ref=f"repo:{self.task_id}" if use_csw else None,
                shape="quick",
                domain_tags=("terminal interface",) if use_csw else ())
            self.csw_candidate_ids = ("tuid",) if use_csw else ()
            bundle = resolve.resolve(
                request, cow_root=(COW if use_csw else ROOT / "statework"),
                requested_flows={"tuid": "discover"} if use_csw else None)
            self.csw_candidate_ids = tuple(sorted({
                str(item.get("id")) for item in bundle.stateworks
                if isinstance(item, Mapping) and item.get("id")}))
            self.cfw_session = runtime.start(
                bundle, telemetry_disabled=True, host_context=host_context,
                runtime_mode="combined_enforcing" if use_csw else "standalone_enforcing",
                **authority_options)
        if self.configuration in DP_CONFIGURATIONS:
            try:
                await self._open_private_dp()
            except BaseException:
                await self._close_runtime_after_open_failure()
                raise
        else:
            if self.configuration == "cfw":
                self.exposure = {"frameworks": ["CFW"],
                                 "cfw_authority_id": str(
                                     self.cfw_session.identity_authority.authorities
                                     .state_authority.authority_identity().get("authority_id", ""))
                                     if self.cfw_session.identity_authority.authorities
                                     .state_authority is not None else "cfw-runtime"}
            elif self.configuration == "cfw_csw":
                scope = _runtime_identity(self.task_id, self.criterion_id).csw_scope()
                snap = self.authority.behavioral_snapshot(
                    workflow_scope=scope, candidate_statework_ids=self.csw_candidate_ids)
                self.exposure = {"frameworks": ["CFW", "CSW"],
                                 "csw_authority_id": str(self.authority.authority_instance_id),
                                 "csw_registry_hash": str(snap.get("registry_hash") or ""),
                                 "csw_snapshot_id": str(snap.get("snapshot_id") or "")}
            else:
                self.exposure = {"frameworks": []}
        return self

    async def _close_runtime_after_open_failure(self) -> None:
        """Release partial host resources when a treatment handshake fails."""
        try:
            if self.bridge is not None:
                await self.bridge.close()
            elif self.client is not None:
                await self.client.close()
        except Exception:
            # Preserve the construction/handshake error that invalidated the
            # treatment while making a best effort to stop its private child.
            pass
        finally:
            close_cfw = getattr(self.cfw_session, "close", None)
            if callable(close_cfw):
                close_cfw()

    async def _open_private_dp(self) -> None:
        if self.cfw_session is None or self.authority is None:
            raise InvalidTreatment("DP cells require the actual CFW+CSW host composition")
        if str(DP) not in sys.path:
            sys.path.insert(0, str(DP))
        from lib.behavior_state import GoalItemSnapshot, GoalSetSnapshot
        from lib.host import MCPControlClient
        from lib.runtime.adapters.cfw import CFWRuntimeAdapter
        from lib.runtime.adapters.csw import CSWRuntimeAdapter, CSWWorkflowBinding
        from lib.runtime.host import RuntimeHost

        self.dp_identity = _runtime_identity(self.task_id, self.criterion_id)
        from host_binding import resolve_bindings
        raw_bindings = resolve_bindings(
            self.authority, self.dp_identity,
            task_context={"candidate_statework_ids": list(self.csw_candidate_ids)})
        bindings = {str(item.identity["statework_id"]): CSWWorkflowBinding(
            identity=dict(item.identity), contract_hash=item.contract_hash)
            for item in raw_bindings}
        if not bindings:
            raise InvalidTreatment("CSW authority exposed no eligible workflow binding")
        self.csw_candidate_ids = tuple(sorted(bindings))
        csw = CSWRuntimeAdapter(self.authority, identity=self.dp_identity,
                                bindings=bindings)
        from digital_psychology_adapter import CFWBehaviorAdapter
        self.cfw_port = RecordingCFWPort(CFWRuntimeAdapter(
            CFWBehaviorAdapter(self.cfw_session, self.cfw_session)))

        # The private control child gets a per-trial state root and never joins
        # the Codex/model MCP registry. The model subprocess gets a sanitized
        # environment separately in agent_codex_host.py.
        dp_env = dict(os.environ)
        dp_env["DIGITALPSYCHOLOGY_ROOT"] = str(DP)
        dp_env["DIGITALPSYCHOLOGY_STATE_ROOT"] = str(self.state_root / "dp-state")
        dp_env["DIGITALPSYCHOLOGY_EVENT_DB"] = str(self.state_root / "dp-events.sqlite3")
        dp_env["DIGITALPSYCHOLOGY_BEHAVIOR_STATE_DB"] = str(
            self.state_root / "dp-behavior.sqlite3")
        self.client = MCPControlClient(
            command=sys.executable, args=("-m", "lib.transport.control_mcp"),
            cwd=DP, env=dp_env)
        runtime_host = RuntimeHost(service=None, model_visible_tools=AGENT_FILE_TOOLS)
        goals = GoalSetSnapshot(revision=0, items=(GoalItemSnapshot(
            criterion_id=self.criterion_id),))
        self.bridge = await runtime_host.open_control_bridge(
            identity=self.dp_identity,
            objective=str(self.task.get("instruction") or "Complete the benchmark task."),
            objective_criteria=(self.criterion_id,), initial_goal_set=goals,
            cfw=self.cfw_port, csw=csw, client=self.client,
            csw_authority_identity=csw.authority_identity(),
            host_policy_context={"deliverable_kind": "code",
                                 "semantic_provider_status": (
                                     "enabled" if self.configuration.endswith("laya")
                                     else "disabled")})
        status = await self.bridge.lifecycle_hooks.open()
        if status.get("status") != "ready" or self.bridge._needs_reconciliation:
            raise InvalidTreatment("private DP stdio host session did not reconcile")
        # This record is fetched through DP's private MCP endpoint. In the Laya
        # arm it proves the running child, rather than local caller metadata,
        # loaded the attested provider.
        self.dp_record = await self.client.behavioral_trial_record(
            self.dp_identity.scope, trial_id=self.task_id)
        if self.configuration.endswith("laya"):
            from lib.telemetry.behavioral_trace import provider_status
            attestation = self.dp_record.get("semantic_provider_attestation")
            required = REQUIRED_PROVIDER_ATTESTATION
            missing = [key for key in required
                       if not isinstance(attestation, Mapping) or not attestation.get(key)]
            if (missing or attestation.get("backend_id") != "digitalpsychology-laya-native"
                    or attestation.get("provider_mode") not in {"shadow", "advisory"}
                    or provider_status(attestation, factory_configured=True)
                    != attestation.get("provider_mode")):
                raise InvalidTreatment(
                    "private DP MCP lacks a live pinned Laya capability handshake")
        self.exposure = {
            "frameworks": ["CFW", "CSW", "DP"] +
                          (["Laya"] if self.configuration.endswith("laya") else []),
            "private_dp_mcp": True,
            "dp_control_session_id": self.dp_identity.scope,
            "dp_behavior_revision": int(status.get("behavior_revision", -1)),
            "csw_authority_id": str(self.authority.authority_instance_id),
            "csw_registry_hash": str(self.dp_record.get("csw_registry_hash") or
                                      self.authority.registry_hash),
            "csw_bindings": list(self.csw_candidate_ids),
            "semantic_provider_attestation": (
                dict(self.dp_record.get("semantic_provider_attestation") or {})),
        }

    async def context_for_turn(self) -> str:
        if self.bridge is not None:
            context = await self.bridge.before_model_call("", tool_capable=True)
        elif self.cfw_session is not None:
            context = self.cfw_session.before_model_call(tool_capable=True)
            if self.authority is not None:
                identity = _runtime_identity(self.task_id, self.criterion_id)
                csw_context, _ = _csw_snapshot_context(
                    self.authority, identity, self.csw_candidate_ids)
                context = f"{context}\n\n{csw_context}"
        else:
            context = "No CFW, CSW, or DP runtime is configured for this baseline cell."
        if not isinstance(context, str) or not context.strip():
            raise HarnessError("selected treatment produced no model context")
        context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
        self.context_hashes.append(context_hash)
        return context

    async def execute_action(self, action: Mapping[str, Any], *, turn: int,
                             action_number: int) -> dict[str, Any]:
        if self.closed:
            raise HarnessError("trial host is closed")
        if not isinstance(action, Mapping):
            raise ValueError("model action proposal must be an object")
        if self.action_count >= MAX_HOST_ACTIONS_PER_TRIAL:
            raise HarnessError("trial host action budget exhausted")
        if action_number >= MAX_HOST_ACTIONS_PER_TURN:
            raise HarnessError("turn host action budget exhausted")
        action = dict(action)
        binding = action.get("tool_binding_id") or action.get("tool")
        args = action.get("arguments") or {}
        if binding not in AGENT_FILE_TOOLS or not isinstance(args, Mapping):
            raise PermissionError("model proposed an unregistered host file action")
        args = dict(args)
        args.setdefault("target", args.get("path"))
        action = {"tool_binding_id": str(binding), "arguments": args,
                  "subject_ref": (self.dp_identity.subject_ref if self.dp_identity
                                  else f"repo:{self.task_id}")}
        action_id = f"{self.task_id}-turn-{turn}-action-{action_number}"
        request_digest = hashlib.sha256(json.dumps(
            action, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        gate_details: dict[str, Any] = {"status": "not_applicable"}
        request_id = None

        if self.bridge is not None:
            from lib.runtime.contracts import ActionCandidate
            candidate = ActionCandidate(
                action_class="read" if binding in {"read", "inspect"} else "write",
                proposed_purpose="DIRECT_GOAL", proposed_reason="model-proposed task action",
                affected_criterion_ids=(self.criterion_id,), action_id=action_id,
                tool_name=str(binding), operation=str(binding),
                mutating=binding == "write", reversible=binding == "write",
                external=False, target_kind="workspace", target_ref_digest=request_digest)
            self.cfw_port.gate_receipts.clear()
            gate_details = await self.bridge.before_action_candidate(
                candidate, cfw_action_request=action)
            request_id = gate_details.get("action_request_id")
            if (gate_details.get("status") != "ready"
                    or gate_details.get("action_authorized") is False):
                receipt = {"receipt_id": action_id, "action_digest": request_digest,
                           "tool": binding, "executed": False,
                           "result": {"status": "blocked", "reason":
                                      str(gate_details.get("reason") or
                                          gate_details.get("status"))},
                           "cfw_gate_receipts": list(self.cfw_port.gate_receipts),
                           "dp_boundary": dict(gate_details)}
                self.receipts.append(receipt)
                return {"receipt": receipt, "output": receipt["result"], "authorized": False}
        elif self.cfw_session is not None:
            prepared = self.cfw_session.prepare_action(action)
            gate_details = {
                "decision": prepared.decision.value,
                "action_request_id": prepared.action_request_id,
                "action_digest": prepared.action_digest,
                "reason_codes": list(prepared.reason_codes),
            }
            request_id = prepared.action_request_id
            if prepared.decision.value != "allow":
                receipt = {"receipt_id": action_id, "action_digest": request_digest,
                           "tool": binding, "executed": False,
                           "result": {"status": "blocked", "reason": prepared.decision.value},
                           "cfw_gate": gate_details}
                self.receipts.append(receipt)
                return {"receipt": receipt, "output": receipt["result"], "authorized": False}

        try:
            actual_result = execute_host_file_action(action, workspace=self.workspace)
        except Exception as exc:
            actual_result = {"status": "fail", "reason": f"{type(exc).__name__}: {exc}"[:256]}
        result_payload = {key: value for key, value in actual_result.items()
                          if key != "content"}
        result_digest = hashlib.sha256(json.dumps(
            actual_result, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        cfw_result_ref = None
        if self.cfw_session is not None:
            cfw_result_ref = self.cfw_session._host_ingress().record_tool_result(
                str(binding), str(actual_result.get("status", "fail")),
                action["subject_ref"], invocation_id=action_id,
                result_digest=result_digest, action_request_id=request_id,
                actual_result={"result_digest": result_digest,
                               "workspace_target": result_payload.get("target")})
            self.cfw_session._host_ingress().complete_action()
        dp_result: Mapping[str, Any] | None = None
        if self.bridge is not None:
            from lib.runtime.contracts import RuntimeObservation
            action_index = max(1, int(self.bridge._action_index) + 1)
            post_observation = RuntimeObservation(
                action_index=action_index, action_completed=True,
                artifact_changed=binding == "write",
                criterion_status={self.criterion_id: "unmet"},
                goal_set=self.bridge.goal_set)
            dp_result = await self.bridge.after_action(post_observation)
            if dp_result.get("status") != "ready":
                raise InvalidTreatment("private DP MCP did not commit the host action boundary")
            self.dp_record = await self.client.behavioral_trial_record(
                self.dp_identity.scope, trial_id=self.task_id)
        self.action_count += 1
        receipt = {
            "receipt_id": action_id, "action_digest": request_digest,
            "tool": binding, "executed": True,
            "result": result_payload, "result_digest": result_digest,
            "cfw_gate": dict(gate_details),
            "cfw_gate_receipts": list(self.cfw_port.gate_receipts)
                                  if self.cfw_port is not None else [],
            "cfw_tool_result_evidence_ref": cfw_result_ref,
            "dp_committed_boundary": dict(dp_result) if dp_result is not None else None,
            "csw_authority_id": str(getattr(self.authority,
                                            "authority_instance_id", "")),
        }
        self.receipts.append(receipt)
        return {"receipt": receipt, "output": actual_result, "authorized": True}

    async def behavioral_record(self, *, trial_id: str) -> dict[str, Any] | None:
        if self.bridge is None:
            return None
        return await self.client.behavioral_trial_record(
            self.dp_identity.scope, trial_id=trial_id)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.bridge is not None:
                await self.bridge.close()
            elif self.client is not None:
                await self.client.close()
        finally:
            close_cfw = getattr(self.cfw_session, "close", None)
            if callable(close_cfw):
                close_cfw()


def build_session(*, configuration: str, task_id: str, state_root: Path,
                  statework_id: str = "tuid",
                  flow_id: str = "discover",
                  provider_manifest: Mapping[str, Any] | None = None
                  ) -> dict[str, Any]:
    """Construct one configuration and return its live session handles.

    Returns the CFW session, the DP session when the configuration binds DP, and
    the CSW authority when configured. Raising is preferred to silently
    substituting a weaker composition.
    """
    if configuration not in {"baseline", "cfw", "cfw_csw", "cfw_csw_dp",
                             "cfw_csw_dp_laya"}:
        raise HarnessError(f"unknown cohort configuration {configuration!r}")
    system_one_service = None
    if configuration == "cfw_csw_dp_laya":
        # Caller-supplied metadata is not evidence that a provider is live.
        # Build the provider from its pinned environment and derive attestation
        # from the capability handshake DP will actually use.
        if str(DP) not in sys.path:
            sys.path.insert(0, str(DP))
        from lib.control.system_one import create_system_one_frame_factory
        from lib.system_one_service import SystemOneService
        from lib.telemetry.behavioral_trace import provider_attestation
        try:
            system_one_service = SystemOneService.from_environment()
            factory = create_system_one_frame_factory(system_one_service)
            attestation = provider_attestation(factory)
            missing = [field for field in REQUIRED_PROVIDER_ATTESTATION
                       if attestation is None or not attestation.get(field)]
            if (missing
                    or attestation.get("backend_id") != "digitalpsychology-laya-native"
                    or attestation.get("provider_mode") not in {"shadow", "advisory"}):
                raise ValueError(
                    "the live provider handshake is incomplete or is not a pinned Laya treatment")
        except Exception as exc:
            if system_one_service is not None:
                system_one_service.close()
            raise InvalidTreatment(
                "the Laya arm lacks an actual enabled provider capability handshake; "
                "INVALID_TREATMENT") from exc

    resolve, runtime = _cfw_modules()
    use_csw = configuration != "baseline"
    authority = None
    kwargs: dict[str, Any] = {}
    if use_csw:
        authority = _real_csw_authority(state_root)
        kwargs["state_authority"] = runtime.create_state_authority(
            authority.root,
            state_path=state_root / "cfw-subjects.json",
            evidence_path=state_root / "cfw-evidence.json")

    request = resolve.TaskRequest(
        task_id=task_id, namespace_id="tenant-cohort",
        application_id="cohort-app", application_instance_id="cohort-instance",
        subject_ref=f"repo:{task_id}" if use_csw else None,
        shape="quick",
        domain_tags=("terminal interface",) if use_csw else ())
    bundle = resolve.resolve(
        request, cow_root=(COW if use_csw else ROOT / "statework"),
        requested_flows={statework_id: flow_id} if use_csw else None)
    cfw_session = runtime.start(bundle, telemetry_disabled=True, **kwargs)

    handles: dict[str, Any] = {
        "configuration": configuration,
        "cfw_session": cfw_session,
        "csw_authority": authority,
        "dp_session": None,
        "resolve": resolve,
        "runtime": runtime,
    }

    if configuration in {"cfw_csw_dp", "cfw_csw_dp_laya"}:
        handles["dp_session"] = _bind_dp(
            cfw_session, task_id=task_id, state_root=state_root,
            statework_id=statework_id, authority=authority,
            system_one_service=system_one_service)
    return handles


def _bind_dp(cfw_session: Any, *, task_id: str, state_root: Path,
             statework_id: str, authority: Any,
             system_one_service: Any = None):
    """Open a real DP RuntimeSession with CFW as its component port."""
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from digital_psychology_adapter import CFWBehaviorAdapter
    from lib.runtime.adapters.cfw import CFWRuntimeAdapter
    from lib.runtime.contracts import RuntimeIdentity
    from lib.runtime.host import RuntimeHost
    from lib.service import DigitalPsychologyService

    identity = RuntimeIdentity(
        namespace_id="tenant-cohort", application_id="cohort-app",
        application_instance_id="cohort-instance", agent_id="cohort-agent",
        agent_instance_id="cohort-agent-1", session_id=f"session-{task_id}",
        task_id=task_id, attempt_id="attempt-1",
        workflow_id=f"workflow-{task_id}", subject_ref=f"repo:{task_id}",
        segment_id="segment-0", behavioral_subject=f"repo:{task_id}")

    service = DigitalPsychologyService(
        event_db=state_root / "dp-events.sqlite3",
        system_one_service=system_one_service)
    host = RuntimeHost(service=service,
                       state_path=state_root / "dp-behavior.sqlite3")
    policy_context = {
        "semantic_provider_status": (
            "enabled" if system_one_service is not None else "disabled")}
    if system_one_service is not None:
        policy_context["deliverable_kind"] = "code"
    # CFWBehaviorAdapter is the production DP-facing port: it owns the signal
    # collection, action gating and context projection DP binds against.
    cfw_port = CFWBehaviorAdapter(cfw_session, cfw_session)
    session = host.open_session(
        identity=identity, objective=f"Complete cohort task {task_id}",
        objective_criteria=(f"{task_id}-criterion",),
        cfw=CFWRuntimeAdapter(cfw_port), csw=None,
        outcome_evidence_authority=None,
        policy_context=policy_context)
    session._cohort_service = service  # keep the service alive with the session
    return session


def drive_actions(handles: Mapping[str, Any], *, criterion_id: str,
                  action_count: int = 3) -> int:
    """Commit real DP actions so the trace reflects actual runtime behavior.

    A session that was merely constructed has nothing to report; telemetry must
    come from actions DP actually committed, not from a zeroed placeholder.
    Returns the number of committed actions.
    """
    dp_session = handles.get("dp_session")
    if dp_session is None:
        return 0
    from lib.runtime.contracts import RuntimeObservation
    committed = 0
    for index in range(1, action_count + 1):
        dp_session.process_action(RuntimeObservation(
            action_index=index, criterion_status={criterion_id: "unmet"}))
        committed += 1
    return committed


def behavioral_record(handles: Mapping[str, Any], *, trial_id: str,
                      task_outcome_score: float | None = None,
                      task_completed: bool | None = None
                      ) -> dict[str, Any] | None:
    """Return DP's internal trial record, or None when DP is not bound.

    A configuration without DP has no behavioral telemetry by construction. That
    is reported as absent rather than faked, so the missing-telemetry tripwire
    can distinguish "DP recorded nothing" from "DP was not configured".
    """
    dp_session = handles.get("dp_session")
    if dp_session is None:
        return None
    return dp_session.behavioral_trial_record(
        trial_id=trial_id, task_outcome_score=task_outcome_score,
        task_completed=task_completed)


__all__ = ["HarnessError", "InvalidTreatment", "REQUIRED_PROVIDER_ATTESTATION",
           "build_session", "drive_actions", "behavioral_record"]
