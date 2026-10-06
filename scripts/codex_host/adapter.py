"""Task lifecycle adapter for host-authorized Codex App Server work."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

import jsonschema
from runtime_contract.actions import ActionDescriptor, default_action_registry
from runtime_contract.task import TaskRequest
from runtime_host.host_api import CognitiveRuntime

from .actions import CodexActionBinding, CodexActionMixin
from .client import JsonlAppServerClient
from .completion import CodexCompletionMixin
from .diagnostics import _native_feature_catalog, _native_feature_categories
from .launch import CodexLaunchProfile
from .protocol import (
    AppServerProtocolError,
    CodexCognitiveHost,
    CodexExecutionPhase,
    _goal_contract_data,
    _safe_value,
)
from .readiness import (
    CodexInferenceReadiness,
    CodexInfrastructureUnavailable,
    inspect_inference_readiness,
)
from .validators import CodexValidatorBinding


class CodexAppServerAdapter(CodexActionMixin, CodexCompletionMixin):
    """Run a host-defined CFW task through Codex App Server.

    Supply dynamic tools and executors from trusted host code. Native model
    tools are disabled by the default launcher; only CFW-authorized tool calls
    can make host-side changes.
    """

    def __init__(self, *, request: TaskRequest,
                 action_bindings: Sequence[CodexActionBinding],
                 validator_bindings: Sequence[CodexValidatorBinding] = (),
                 model: str, cwd: str | Path,
                 runtime: CognitiveRuntime | None = None,
                 cognitive_host: CodexCognitiveHost | None = None,
                 runtime_mode: str = "standalone_enforcing",
                 task_start_options: Mapping[str, Any] | None = None,
                 client_name: str = "cognitiveframeworks",
                 client_title: str = "CognitiveFrameWorks Codex Host",
                 client_version: str = "1.0.0",
                 approval_handler: Callable[[Mapping[str, Any]], Any] | None = None,
                 event_handler: Callable[[str, Mapping[str, Any]], Any] | None = None,
                 launch_profile: CodexLaunchProfile | None = None,
                 launch_environment: Mapping[str, str] | None = None,
                 session_nonce: str | None = None,
                 expected_session_id: str | None = None) -> None:
        if not isinstance(request, TaskRequest):
            raise TypeError("request must be a host-authored TaskRequest")
        if not isinstance(model, str) or not model.strip() or len(model) > 128:
            raise ValueError("model must be bounded host-selected text")
        workspace = Path(cwd).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("Codex working directory must exist and be a directory")
        bindings = tuple(action_bindings)
        if not bindings or len(bindings) > 64:
            raise ValueError("provide between one and 64 host-owned action bindings")
        if any(not isinstance(item, CodexActionBinding) for item in bindings):
            raise TypeError("action_bindings must contain CodexActionBinding values")
        if len({item.name for item in bindings}) != len(bindings):
            raise ValueError("Codex dynamic tool names must be unique")
        if len({item.action_binding_id for item in bindings}) != len(bindings):
            raise ValueError("CFW action binding IDs must be unique")
        validators = tuple(validator_bindings)
        if len(validators) > 64 or any(
                not isinstance(item, CodexValidatorBinding) for item in validators):
            raise ValueError("validator_bindings must contain at most 64 host validators")
        if len({item.validator_id for item in validators}) != len(validators):
            raise ValueError("Codex host validator IDs must be unique")
        bindings_by_id = {item.action_binding_id: item for item in bindings}
        goal_data = _goal_contract_data(request)
        criteria = {
            str(item.get("criterion_id")): item
            for item in goal_data.get("acceptance_criteria", ())
            if isinstance(item, Mapping) and isinstance(item.get("criterion_id"), str)
        }
        if validators and not criteria:
            raise ValueError("host validators require a host-authored task goal contract")
        covered_criteria: set[str] = set()
        for validator_binding in validators:
            action_binding = bindings_by_id.get(validator_binding.action_binding_id)
            if action_binding is None:
                raise ValueError(
                    f"validator {validator_binding.validator_id!r} references an unknown action")
            try:
                action_schema = json.loads(action_binding._schema_json)
                jsonschema.validators.validator_for(action_schema)(
                    action_schema).validate(validator_binding.arguments_value())
            except jsonschema.ValidationError as exc:
                raise ValueError(
                    f"validator {validator_binding.validator_id!r} arguments do not match "
                    "its host action schema") from exc
            if set(validator_binding.criterion_ids) - set(criteria):
                raise ValueError(
                    f"validator {validator_binding.validator_id!r} references an unknown criterion")
            invalid_coverage = {
                criterion_id: sorted(
                    set(validator_binding.covered_boundary_ids)
                    - {str(boundary) for boundary in
                       criteria[criterion_id].get("required_boundaries", ())})
                for criterion_id in validator_binding.criterion_ids
                if set(validator_binding.covered_boundary_ids)
                - {str(boundary) for boundary in
                   criteria[criterion_id].get("required_boundaries", ())}
            }
            if invalid_coverage:
                raise ValueError(
                    f"validator {validator_binding.validator_id!r} has boundary IDs "
                    f"undeclared for its criteria: {invalid_coverage}")
            covered_criteria.update(validator_binding.criterion_ids)
        validator_boundaries: dict[str, set[str]] = {}
        for validator_binding in validators:
            for criterion_id in validator_binding.criterion_ids:
                validator_boundaries.setdefault(criterion_id, set()).update(
                    validator_binding.covered_boundary_ids)
        required_criteria = {
            criterion_id for criterion_id, item in criteria.items()
            if item.get("required", True)}
        if validators and not required_criteria.issubset(covered_criteria):
            missing = sorted(required_criteria - covered_criteria)
            raise ValueError(
                f"host validators do not cover required task criteria: {missing}")
        for criterion_id in required_criteria:
            required_boundaries = set(criteria[criterion_id].get("required_boundaries", ()))
            missing_boundaries = sorted(
                required_boundaries - validator_boundaries.get(criterion_id, set()))
            if missing_boundaries:
                raise ValueError(
                    f"host validators do not cover required boundaries for "
                    f"{criterion_id!r}: {missing_boundaries}")
        if session_nonce is not None and (
                not isinstance(session_nonce, str) or not session_nonce.strip()
                or len(session_nonce) > 256):
            raise ValueError("session_nonce must be bounded host-held text")
        if expected_session_id is not None and (
                not isinstance(expected_session_id, str) or not expected_session_id.strip()
                or len(expected_session_id) > 256):
            raise ValueError("expected_session_id must be a bounded stable App Server ID")
        if (session_nonce is None) != (expected_session_id is None):
            raise ValueError("resume requires both the host session_nonce and stable session ID")
        if task_start_options is not None and not isinstance(task_start_options, Mapping):
            raise TypeError("task_start_options must be a host-authored mapping")
        reserved_start_options = {
            "session_id", "session_nonce", "resume", "runtime_mode",
            "telemetry_disabled",
        }
        extra_start_options = dict(task_start_options or {})
        if (len(extra_start_options) > 32
                or set(extra_start_options) & reserved_start_options
                or any(not isinstance(key, str) for key in extra_start_options)):
            raise ValueError("task_start_options contain reserved or excessive fields")

        base_registry = default_action_registry()
        extensions: dict[str, ActionDescriptor] = {}
        for binding in bindings:
            existing = base_registry.get(binding.action_binding_id)
            if existing is not None:
                if existing != binding.descriptor:
                    raise ValueError(
                        f"built-in CFW action {binding.action_binding_id!r} cannot be redefined")
            else:
                extensions[binding.action_binding_id] = binding.descriptor
        self.runtime = runtime or CognitiveRuntime(action_registry=extensions)
        for binding in bindings:
            registered = self.runtime.action_registry.get(binding.action_binding_id)
            if registered != binding.descriptor:
                raise ValueError(
                    f"CFW runtime lacks the host action binding {binding.action_binding_id!r}")

        self.request = request
        self.action_bindings = {item.name: item for item in bindings}
        self._action_bindings_by_id = bindings_by_id
        self.validator_bindings = validators
        self._validation_epochs: dict[str, Any] = {}
        self.model = model
        self.cwd = workspace
        self.cognitive_host = cognitive_host
        self.runtime_mode = runtime_mode
        self.task_start_options = extra_start_options
        self.client_name = client_name
        self.client_title = client_title
        self.client_version = client_version
        self.approval_handler = approval_handler
        self.event_handler = event_handler
        self.launch_profile = launch_profile
        self.launch_environment = dict(launch_environment or {})
        if set(self.launch_environment) - set(
                launch_profile.secret_env_keys if launch_profile else ()):
            raise ValueError("launch_environment contains undeclared secret keys")
        self._client: JsonlAppServerClient | None = None
        self._thread_id: str | None = None
        self._app_server_session_id: str | None = None
        self._app_server_identity_kind: str | None = None
        self._task_handle: Any = None
        self._host: Any = None
        self._session_nonce = session_nonce or secrets.token_urlsafe(32)
        self._resume = session_nonce is not None
        self._expected_session_id = expected_session_id
        self._cfw_session_id = self._stable_cfw_session_id()
        self._cognitive_fault: str | None = None
        self._cognitive_fault_detail: str | None = None
        self._behavior_trace: list[dict[str, Any]] = []
        self._cfw_gate_receipts: list[dict[str, Any]] = []
        self._active_turn_id: str | None = None
        self._active_attempt_ref: str | None = None
        self._active_ready = asyncio.Event()
        self._turn_completion: asyncio.Future[dict[str, Any]] | None = None
        self._early_completion: dict[str, Any] | None = None
        self._early_turn_event: dict[str, Any] | None = None
        self._last_turn_status: str | None = None
        self._last_assistant_message = ""
        self._pending_native_actions: dict[str, list[dict[str, Any]]] = {}
        self._file_change_items: dict[str, Mapping[str, Any]] = {}
        self._tool_call_responses: dict[str, dict[str, Any]] = {}
        self._action_lock = asyncio.Lock()
        self._completion_lock = asyncio.Lock()
        self._closed = False
        self._cognitive_open_attempted = False
        self._inference_readiness: CodexInferenceReadiness | None = None
        self._execution_phase = CodexExecutionPhase.PROCESS_STARTED
        self._model_execution_observed = False
        self._action_activity_observed = False
        self._validator_activity_observed = False
        self._completion_checked = False
        self._cognitive_close_completed = False

    def _advance_phase(self, phase: CodexExecutionPhase) -> None:
        self._execution_phase = max(self._execution_phase, phase)

    @property
    def dynamic_tools(self) -> list[dict[str, Any]]:
        return [binding.app_server_spec() for binding in self.action_bindings.values()]

    def initialize_params(self) -> dict[str, Any]:
        return {
            "clientInfo": {"name": self.client_name, "title": self.client_title,
                           "version": self.client_version},
            "capabilities": {"experimentalApi": True},
        }

    def thread_start_params(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "cwd": str(self.cwd),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "dynamicTools": self.dynamic_tools,
            "developerInstructions": (
                "Use only the host-provided cfw_ tools for task actions. "
                "The host owns the task contract, action authorization, and completion. "
                "Do not claim completion without asking the host validator."),
        }

    def _stable_cfw_session_id(self) -> str:
        identity = hashlib.sha256(self._session_nonce.encode("utf-8")).hexdigest()[:32]
        return f"codex-cfw-{identity}"

    def _bound_request(self) -> TaskRequest:
        # CFW identity is fixed before Codex creates a thread. Transport
        # identities are attached separately after thread/start or resume.
        identity_suffix = hashlib.sha256(
            self._session_nonce.encode("utf-8")).hexdigest()[:24]
        suffix = f":codex-{identity_suffix}"
        agent_instance = f"{self.request.agent_id[:256 - len(suffix)]}{suffix}"
        interaction_id = self.request.interaction_id or (
            f"codex-task:{self.request.task_id}")
        return replace(self.request, agent_instance_id=agent_instance,
                       interaction_id=interaction_id)

    async def _cognitive_call(self, method: str, **kwargs: Any) -> Any:
        if self.cognitive_host is None:
            return None
        callback = getattr(self.cognitive_host, method, None)
        if not callable(callback):
            raise RuntimeError(  # noqa: TRY004 - this is a missing host API boundary
                f"Codex cognitive host lacks required {method} boundary")
        result = callback(**kwargs)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _require_boundary_ready(name: str, result: Any) -> None:
        if result is False:
            raise RuntimeError(f"Codex cognitive host rejected {name}")
        if isinstance(result, Mapping):
            if result.get("ready") is False or result.get("action_authorized") is False:
                raise RuntimeError(f"Codex cognitive host rejected {name}")
            if result.get("status") in {"blocked", "degraded", "failed", "incomplete"}:
                raise RuntimeError(f"Codex cognitive host rejected {name}")

    async def open(self, client: JsonlAppServerClient | None = None, *,
                   thread_id: str | None = None,
                   resume: bool | None = None) -> dict[str, Any]:
        if self._client is not None:
            raise RuntimeError("Codex App Server adapter is already open")
        is_resume = self._resume if resume is None else bool(resume)
        if is_resume and not self._resume:
            raise ValueError("resuming CFW requires the persisted host recovery identity")
        if self._resume and resume is False:
            raise ValueError("a persisted host recovery identity cannot start a new CFW task")
        if is_resume and (not thread_id or not self._expected_session_id):
            raise ValueError("resume requires the persisted thread_id and stable session ID")
        if client is not None:
            self._client = client
        elif self.launch_profile is not None:
            self._client = await JsonlAppServerClient.launch(
                cwd=self.cwd, launch_profile=self.launch_profile,
                launch_environment=self.launch_environment)
        else:
            self._client = await JsonlAppServerClient.launch(cwd=self.cwd)
        self._client.set_handlers(request_handler=self.handle_server_request,
                                  notification_handler=self.handle_notification)
        try:
            await self._client.request("initialize", self.initialize_params())
            await self._client.notify("initialized")
            self._advance_phase(CodexExecutionPhase.INITIALIZED)
            self._inference_readiness = await inspect_inference_readiness(
                self._client, self.launch_profile)
            if not self._inference_readiness.ready:
                raise CodexInfrastructureUnavailable(self._inference_readiness)
            self._advance_phase(CodexExecutionPhase.INFERENCE_READY)
            request = self._bound_request()
            self._task_handle = self.runtime.begin_task(
                request, session_id=self._cfw_session_id,
                session_nonce=self._session_nonce, resume=is_resume,
                runtime_mode=self.runtime_mode, **self.task_start_options)
            self._host = self.runtime.host_handle(self._task_handle)
            if self.cognitive_host is not None:
                self._cognitive_open_attempted = True
                opened = await self._cognitive_call(
                    "open_task", request=request, runtime=self.runtime,
                    task_handle=self._task_handle, host_handle=self._host,
                    session_id=self._cfw_session_id,
                    session_nonce=self._session_nonce, resume=is_resume,
                    runtime_mode=self.runtime_mode,
                    thread_id=thread_id if is_resume else None,
                    app_server_session_id=(self._expected_session_id
                                           if is_resume else None))
                self._require_boundary_ready("task open", opened)
                if is_resume:
                    reconciled = await self._cognitive_call("reconcile")
                    self._require_boundary_ready("resume reconciliation", reconciled)
                else:
                    started = await self._cognitive_call("task_started")
                    self._require_boundary_ready("task start", started)
            method = "thread/resume" if is_resume else "thread/start"
            params = ({"threadId": thread_id, **self.thread_start_params()}
                      if is_resume else self.thread_start_params())
            response = await self._client.request(method, params)
            thread = response.get("thread") if isinstance(response, Mapping) else None
            if not isinstance(thread, Mapping):
                raise AppServerProtocolError("Codex App Server omitted the thread identity")
            resolved_thread_id = thread.get("id")
            if not isinstance(resolved_thread_id, str) or not resolved_thread_id:
                raise AppServerProtocolError("Codex App Server returned an invalid thread ID")
            raw_session_id = thread.get("sessionId")
            if raw_session_id is None:
                # Older generic App Server versions expose a durable thread ID
                # but no separate session ID. Keep recovery bound to that ID.
                session_id = resolved_thread_id
                identity_kind = "thread"
            elif isinstance(raw_session_id, str) and raw_session_id:
                session_id = raw_session_id
                identity_kind = "session"
            else:
                raise AppServerProtocolError(
                    "Codex App Server returned an invalid sessionId")
            if len(resolved_thread_id) > 256 or len(session_id) > 256:
                raise AppServerProtocolError("Codex App Server returned oversized identity values")
            if is_resume and resolved_thread_id != thread_id:
                raise AppServerProtocolError("resumed App Server thread identity changed")
            if is_resume and session_id != self._expected_session_id:
                raise AppServerProtocolError("resumed App Server session identity changed")
            self._thread_id = resolved_thread_id
            self._app_server_session_id = session_id
            self._app_server_identity_kind = identity_kind
            self._advance_phase(CodexExecutionPhase.THREAD_OPEN)
            if self.cognitive_host is not None:
                attached = await self._cognitive_call(
                    "bind_transport_identity", thread_id=resolved_thread_id,
                    app_server_session_id=session_id)
                self._require_boundary_ready("Codex transport identity binding", attached)
            return self.status()
        except BaseException:
            if self.cognitive_host is not None and self._cognitive_open_attempted:
                with suppress(Exception):
                    await self._cognitive_call("close")
            if self._task_handle is not None:
                with suppress(Exception):
                    self._task_handle.close()
                self._task_handle = None
                self._host = None
            with suppress(Exception):
                await self._client.close()
            self._client = None
            raise

    def recovery_record(self) -> dict[str, str]:
        """Return host-only identity material for a protected persistence store."""
        if not self._thread_id or not self._app_server_session_id:
            raise RuntimeError("adapter must be open before exporting recovery identity")
        return {
            "thread_id": self._thread_id,
            "app_server_session_id": self._app_server_session_id,
            "app_server_identity_kind": self._app_server_identity_kind or "unknown",
            "cfw_session_id": self._task_handle.session_id,
            "task_id": self.request.task_id,
            "session_nonce": self._session_nonce,
        }

    def _goal_context(self) -> str:
        contract_data = _goal_contract_data(self.request)
        if not contract_data:
            return (f"Host task id: {self.request.task_id}\n"
                    f"Host subject: {self.request.subject_ref or 'unspecified'}")
        criteria = contract_data.get("acceptance_criteria", ())
        lines = [f"Objective: {contract_data.get('objective', '')}",
                 f"Deliverable: {contract_data.get('deliverable_kind', '')}",
                 "Host-owned acceptance criteria:"]
        for item in criteria:
            if isinstance(item, Mapping):
                lines.append(f"- {item.get('criterion_id')}: {item.get('claim')}")
        return "\n".join(lines)

    async def run_turn(self, user_text: str, *, timeout: float = 900.0) -> dict[str, Any]:
        if self._client is None or self._host is None or not self._thread_id:
            raise RuntimeError("adapter must be open before starting a turn")
        if self._completion_lock.locked():
            raise RuntimeError("Codex task validation is in progress")
        if self._cognitive_fault is not None:
            raise RuntimeError(
                "Codex cognitive host requires reconciliation before another turn")
        if self._active_turn_id is not None or self._turn_completion is not None:
            raise RuntimeError("only one Codex turn may run per host task at a time")
        if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 64_000:
            raise ValueError("user_text must be non-empty and bounded")
        if self.cognitive_host is None:
            context = self._task_handle.before_model_call(tool_capable=True)
        else:
            context = self._task_handle.base_model_context()
            context = await self._cognitive_call(
                "before_model_call", context=context, tool_capable=True)
            if not isinstance(context, str):
                raise RuntimeError("Codex cognitive host returned invalid model context")
        guidance_bytes = context.encode("utf-8")
        behavioral_snapshot: Mapping[str, Any] = {}
        snapshot_provider = getattr(self._task_handle, "behavioral_trace_snapshot", None)
        if callable(snapshot_provider):
            with suppress(Exception):
                candidate = snapshot_provider()
                if isinstance(candidate, Mapping):
                    behavioral_snapshot = candidate
        trace = {
            "turn_index": len(self._behavior_trace) + 1,
            "guidance_byte_count": len(guidance_bytes),
            "guidance_sha256": hashlib.sha256(guidance_bytes).hexdigest(),
            **dict(behavioral_snapshot),
        }
        self._behavior_trace.append(trace)
        del self._behavior_trace[:-16]
        full_input = (f"{user_text}\n\nHOST TASK CONTRACT\n{self._goal_context()}"
                      f"\n\nCFW RUNTIME GUIDANCE\n{context}")
        self._active_ready.clear()
        self._turn_completion = asyncio.get_running_loop().create_future()
        self._early_completion = None
        self._early_turn_event = None
        self._tool_call_responses.clear()
        self._file_change_items.clear()
        self._last_assistant_message = ""
        try:
            response = await self._client.request("turn/start", {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": full_input}],
                "cwd": str(self.cwd),
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
            })
            turn = response.get("turn") if isinstance(response, Mapping) else None
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise AppServerProtocolError("turn/start omitted the Codex turn ID")
            self._active_turn_id = turn_id
            self._advance_phase(CodexExecutionPhase.TURN_ACCEPTED)
            self._active_attempt_ref = self._begin_attempt(turn_id, full_input)
            self._active_ready.set()
            if (self._early_completion is not None
                    and self._early_completion.get("turn_id") == turn_id):
                if self._early_turn_event is not None:
                    self._finish_turn_notification(
                        self._early_turn_event, self._early_completion)
                else:
                    self._turn_completion.set_result(self._early_completion)
            result = await asyncio.wait_for(self._turn_completion, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            if self._active_turn_id:
                with suppress(Exception):
                    await self._client.request("turn/interrupt", {
                        "threadId": self._thread_id,
                        "turnId": self._active_turn_id,
                    }, timeout=10.0)
            if self._active_attempt_ref:
                self._record_turn_failure({
                    "id": self._active_turn_id,
                    "status": "interrupted",
                    "error": {"message": "host turn timeout"},
                })
            raise
        except Exception as exc:
            if self._active_attempt_ref:
                self._record_turn_failure({
                    "id": self._active_turn_id,
                    "status": "failed",
                    "error": {"message": type(exc).__name__},
                })
            raise
        finally:
            self._settle_pending_native_actions("failure")
            self._file_change_items.clear()
            self._active_ready.set()
            if self._turn_completion is not None and not self._turn_completion.done():
                self._turn_completion.cancel()
            self._active_turn_id = None
            self._active_attempt_ref = None
            self._turn_completion = None

    def _begin_attempt(self, turn_id: str, prompt: str) -> str:
        attempt_ref = f"codex-turn:{turn_id}"
        if len(attempt_ref) > 240:
            attempt_ref = "codex-turn:" + hashlib.sha256(turn_id.encode()).hexdigest()
        cause_id = f"codex:{self._thread_id}:{turn_id}"
        if len(cause_id) > 240:
            cause_id = "codex:" + hashlib.sha256(cause_id.encode()).hexdigest()
        premise = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self._host.begin_attempt(
            attempt_ref=attempt_ref, cause_id=cause_id,
            premise_digest=premise,
            action_plan={"harness": "codex-app-server",
                         "thread_id": self._thread_id,
                         "turn_id": turn_id,
                         "tool_names": sorted(self.action_bindings)},
        )
        return attempt_ref












    async def reconcile(self) -> Mapping[str, Any]:
        """Reconcile the cognitive lifecycle before admitting more work."""
        if self.cognitive_host is None:
            self._cognitive_fault = None
            return {"status": "ready", "reconciled": True}
        result = await self._cognitive_call("reconcile")
        self._require_boundary_ready("reconciliation", result)
        if isinstance(result, Mapping) and result.get("status") not in {
                None, "ready", "reconciled", "ok"}:
            raise RuntimeError("Codex cognitive host did not reach ready state")
        self._cognitive_fault = None
        return {"status": "ready", "reconciled": True}

    async def interrupt(self) -> None:
        if self._client is None or not self._thread_id or not self._active_turn_id:
            return
        await self._client.request("turn/interrupt", {
            "threadId": self._thread_id, "turnId": self._active_turn_id})

    def status(self) -> dict[str, Any]:
        host_policy_verified = bool(
            self._client is not None
            and getattr(self._client, "host_policy_verified", False))
        cognitive_status: dict[str, Any] | None = None
        if self.cognitive_host is not None:
            snapshot_provider = getattr(self.cognitive_host, "status_snapshot", None)
            if callable(snapshot_provider):
                try:
                    snapshot = snapshot_provider()
                    if isinstance(snapshot, Mapping):
                        bounded = _safe_value(snapshot)
                        if isinstance(bounded, dict):
                            cognitive_status = bounded
                except Exception:  # noqa: BLE001 - status telemetry must fail closed to unavailable
                    cognitive_status = {"status": "unavailable"}
        return {
            "connected": self._client is not None and not self._closed,
            "task_id": self.request.task_id,
            "runtime_mode": self.runtime_mode,
            "thread_id": self._thread_id,
            "app_server_session_id": self._app_server_session_id,
            "app_server_identity_kind": self._app_server_identity_kind,
            "cfw_session_id": (self._task_handle.session_id
                               if self._task_handle is not None else None),
            "active_turn_id": self._active_turn_id,
            "last_turn_status": self._last_turn_status,
            "inference_ready": (self._inference_readiness.ready
                                if self._inference_readiness is not None else False),
            "inference_readiness": (self._inference_readiness.to_dict()
                                    if self._inference_readiness is not None else None),
            "model_execution_observed": self._model_execution_observed,
            "action_activity_observed": self._action_activity_observed,
            "validator_activity_observed": self._validator_activity_observed,
            "completion_checked": self._completion_checked,
            "behavior_trace": [dict(item) for item in self._behavior_trace],
            "cfw_gate_receipts": [dict(item) for item in self._cfw_gate_receipts[:128]],
            "execution_phase": self._execution_phase.name.lower(),
            "registered_tool_count": len(self.action_bindings),
            "registered_validator_count": len(self.validator_bindings),
            "model_visible_dp_tools": 0 if host_policy_verified else None,
            "model_visibility_verified": host_policy_verified,
            "native_mutation_surfaces_closed": (
                True if host_policy_verified else None),
            "native_action_features_supported": (
                list(getattr(self._client, "supported_native_features", ()))
                if host_policy_verified and self._client is not None else None),
            "native_action_features_disabled": (
                list(getattr(self._client, "disabled_native_features", ()))
                if host_policy_verified and self._client is not None else None),
            "native_action_feature_categories": (
                _native_feature_categories(
                    getattr(self._client, "supported_native_features", ()),
                    getattr(self._client, "disabled_native_features", ()))
                if host_policy_verified and self._client is not None else None),
            "native_action_feature_catalog": _native_feature_catalog(
                getattr(self._client, "supported_native_features", ()),
                getattr(self._client, "disabled_native_features", ()),
                getattr(self._client, "codex_feature_catalog", None)),
            "cognitive_host_connected": self.cognitive_host is not None,
            "cognitive_host_status": cognitive_status,
            "cognitive_fault": self._cognitive_fault,
            "cognitive_fault_detail": self._cognitive_fault_detail,
            "internal_inference_boundaries_intercepted": False,
            "completion_requires_host_validator": True,
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._advance_phase(CodexExecutionPhase.TERMINAL)
        try:
            if self._client is not None:
                if self._active_turn_id:
                    with suppress(Exception):
                        await self.interrupt()
                await self._client.close()
        finally:
            try:
                if (self.cognitive_host is not None
                        and self._cognitive_open_attempted
                        and not self._cognitive_close_completed):
                    with suppress(Exception):
                        await self._cognitive_call("close")
                    self._cognitive_close_completed = True
                if self._task_handle is not None:
                    self._task_handle.close()
            finally:
                self._task_handle = None
                self._client = None
