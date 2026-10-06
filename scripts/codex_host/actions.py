"""Host-owned dynamic actions and App Server event handling."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from runtime_contract.actions import ActionDescriptor
from runtime_contract.validators import evaluate_validator
from runtime_session.lifecycle import GateDecision

from .protocol import (
    _TOOL_NAME,
    MAX_TOOL_SCHEMA_BYTES,
    AppServerRPCError,
    CodexExecutionPhase,
    _json_bytes,
    _public_json,
    _safe_error,
    _safe_text,
    _safe_value,
)
from .validators import CodexValidatorBinding

# Executor and host boundaries fail closed on any unhandled collaborator error.
# ruff: noqa: BLE001


@dataclass(frozen=True)
class CodexActionBinding:
    """One host-owned dynamic tool and its CFW action binding."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    action_binding_id: str
    descriptor: ActionDescriptor
    executor: Callable[[Mapping[str, Any]], Any | Awaitable[Any]]
    _schema_json: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError("Codex dynamic tool names must use the cfw_ prefix")
        if (not isinstance(self.description, str) or not self.description.strip()
                or len(self.description) > 1024):
            raise ValueError("dynamic tool description must be bounded non-empty text")
        if (not isinstance(self.action_binding_id, str) or not self.action_binding_id
                or len(self.action_binding_id) > 128):
            raise ValueError("action_binding_id must be a bounded registered CFW binding")
        if not isinstance(self.input_schema, Mapping):
            raise TypeError("dynamic tool input_schema must be a JSON schema object")
        if self.input_schema.get("type") != "object":
            raise ValueError("dynamic tool input_schema must describe an object")
        try:
            schema_json = json.dumps(dict(self.input_schema), sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False)
            schema = json.loads(schema_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("dynamic tool input_schema must contain only JSON values") from exc
        if len(schema_json.encode("utf-8")) > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("dynamic tool input_schema exceeds the size bound")
        try:
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise ValueError("dynamic tool input_schema is invalid JSON Schema") from exc
        if not callable(self.executor):
            raise TypeError("dynamic tool executor must be callable")
        object.__setattr__(self, "_schema_json", schema_json)

    def app_server_spec(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": self.description,
                "inputSchema": json.loads(self._schema_json)}


class CodexActionMixin:
    async def handle_server_request(self, method: str,
                                    params: Mapping[str, Any]) -> Any:
        if method == "item/tool/call":
            self._action_activity_observed = True
            self._advance_phase(CodexExecutionPhase.ACTION_ACTIVITY_OBSERVED)
            await self._active_ready.wait()
            call_id = params.get("callId")
            if (not self._matches_active_turn(params)
                    or not isinstance(call_id, str) or not call_id.strip()
                    or len(call_id) > 256):
                return {"contentItems": [{"type": "inputText",
                                          "text": "Host rejected an invalid tool call identity."}],
                        "success": False}
            async with self._action_lock:
                cached = self._tool_call_responses.get(call_id)
                if cached is not None:
                    return dict(cached)
                if len(self._tool_call_responses) >= 256:
                    return {"contentItems": [{"type": "inputText",
                                              "text": "Host tool call limit reached for this turn."}],
                            "success": False}
                result = await self._handle_dynamic_tool(params)
                self._tool_call_responses[call_id] = dict(result)
                return result
        if method in {"item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval",
                      "item/applyPatch/requestApproval"}:
            await self._active_ready.wait()
            if not self._matches_active_turn(params):
                return {"decision": "decline"}
            # Cognitive hosts currently authorize only the explicit dynamic
            # cfw_ action registry. Native mutation surfaces stay closed until
            # they have corresponding pre-action and post-action boundaries.
            if self.cognitive_host is not None:
                return {"decision": "decline"}
        async with self._action_lock:
            if method == "item/commandExecution/requestApproval":
                return await self._handle_command_approval(params)
            if method == "item/fileChange/requestApproval":
                return await self._handle_file_change_approval(params)
            if method == "item/applyPatch/requestApproval":
                return {"decision": "decline"}
        raise AppServerRPCError(-32601, "Server request is not supported by the CFW host")
    def _matches_active_turn(self, params: Mapping[str, Any]) -> bool:
        return bool(self._active_turn_id and self._thread_id
                    and params.get("threadId") == self._thread_id
                    and params.get("turnId") == self._active_turn_id)
    def _registered_validator_for_action(
            self, binding: CodexActionBinding,
            arguments: Mapping[str, Any]) -> CodexValidatorBinding | None:
        """Match only an exact host-authored validator invocation."""
        matches = [item for item in self.validator_bindings
                   if item.action_binding_id == binding.action_binding_id
                   and item.arguments_value() == dict(arguments)]
        if len(matches) > 1:
            raise RuntimeError("dynamic action ambiguously matches host validators")
        return matches[0] if matches else None
    async def _handle_dynamic_tool(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(params.get("arguments"), Mapping):
            return {"contentItems": [{"type": "inputText",
                                      "text": "Host rejected malformed tool arguments."}],
                    "success": False}
        binding = self.action_bindings.get(str(params.get("tool") or ""))
        if binding is None:
            return {"contentItems": [{"type": "inputText",
                                      "text": "Host rejected an unregistered tool."}],
                    "success": False}
        arguments = dict(params["arguments"])
        schema = json.loads(binding._schema_json)
        validator = jsonschema.validators.validator_for(schema)
        try:
            validator(schema).validate(arguments)
        except jsonschema.ValidationError:
            return {"contentItems": [{"type": "inputText",
                                      "text": "Host rejected arguments outside the tool schema."}],
                    "success": False}
        validator_binding = self._registered_validator_for_action(binding, arguments)
        subject_ref = self.request.subject_ref or (
            self.request.task_id if validator_binding is not None else None)
        action = {"tool_binding_id": binding.action_binding_id,
                  "arguments": arguments, "subject_ref": subject_ref}
        call_id = str(params.get("callId") or "")
        if not call_id:
            return {"contentItems": [{"type": "inputText",
                                      "text": "Host rejected a missing invocation identity."}],
                    "success": False}
        if self._cognitive_fault is not None:
            return {"contentItems": [{"type": "inputText",
                                      "text": "Host paused actions pending cognitive reconciliation."}],
                    "success": False}
        if self.cognitive_host is None:
            preflight_tool = getattr(self._host, "preflight_tool", None)
            if callable(preflight_tool):
                preflight = preflight_tool(action)
                self._cfw_gate_receipts.append({
                    "stage": "preflight",
                    "invocation_id": call_id[:128],
                    "action_digest": str(preflight.action_digest)[:64],
                    "decision": preflight.decision.value,
                    "reason_codes": list(preflight.reason_codes)[:8],
                    "cfw_revision": getattr(
                        self._task_handle, "cfw_behavior_revision", None),
                })
                if preflight.decision != GateDecision.ALLOW:
                    return {"contentItems": [{
                        "type": "inputText",
                        "text": "Host policy blocked this action."}],
                        "success": False}
            prepared = self._host.prepare_tool(action)
            decision = prepared.decision
            action_request_id = prepared.action_request_id
            reason_codes = list(prepared.reason_codes)[:8]
            self._cfw_gate_receipts.append({
                "stage": "final", "invocation_id": call_id[:128],
                "action_request_id": str(action_request_id or "")[:128],
                "action_digest": str(getattr(prepared, "action_digest", ""))[:64],
                "decision": decision.value, "reason_codes": reason_codes,
                "cfw_revision": getattr(
                    self._task_handle, "cfw_behavior_revision", None),
            })
        else:
            try:
                prepared = await self._cognitive_call(
                    "before_action", action=action,
                    binding=binding.action_binding_id, invocation_id=call_id)
            except Exception:
                self._cognitive_fault = "pre_action_boundary_failed"
                return {"contentItems": [{"type": "inputText",
                                          "text": "Host control could not authorize this action."}],
                        "success": False}
            if not isinstance(prepared, Mapping):
                self._cognitive_fault = "pre_action_boundary_malformed"
                return {"contentItems": [{"type": "inputText",
                                          "text": "Host control returned an invalid action decision."}],
                        "success": False}
            if prepared.get("action_authorized") is not True:
                return {"contentItems": [{"type": "inputText",
                                          "text": "Host policy blocked this action."}],
                        "success": False}
            decision = GateDecision.ALLOW
            action_request_id = prepared.get("action_request_id")
            reason_codes = []
            if not isinstance(action_request_id, str) or not action_request_id:
                self._cognitive_fault = "pre_action_correlation_missing"
                return {"contentItems": [{"type": "inputText",
                                          "text": "Host control omitted action correlation."}],
                        "success": False}
        if decision != GateDecision.ALLOW:
            self._host.observation(
                "codex.action.blocked", subject_ref,
                details={"tool": binding.name,
                         "decision": decision.value,
                         "reason_codes": reason_codes})
            return {"contentItems": [{"type": "inputText",
                                      "text": "Host policy blocked this action."}],
                    "success": False}
        artifact_digest_before = (
            self._validation_artifact_digest()
            if validator_binding is not None else None)
        try:
            result = binding.executor(arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            failure_digest = hashlib.sha256(b"codex-host-tool-failed").hexdigest()
            self._host.after_tool(
                tool_type=binding.descriptor.tool_type, result_class="failure",
                subject_ref=subject_ref, invocation_id=call_id,
                action_request_id=action_request_id,
                result_digest=failure_digest,
                actual_result={"success": False, "failure_class": "host_tool_error"})
            self._host.complete_action()
            if self.cognitive_host is not None:
                try:
                    await self._cognitive_call(
                        "after_action", action=action,
                        binding=binding.action_binding_id, invocation_id=call_id,
                        action_request_id=action_request_id,
                        actual_result={"success": False,
                                       "failure_class": "host_tool_error"},
                        result_digest=failure_digest, evidence_ref="")
                except Exception as exc:
                    self._cognitive_fault = "post_action_boundary_failed"
                    self._cognitive_fault_detail = _safe_text(
                        f"{type(exc).__name__}: {exc}", 256)
            return {"contentItems": [{"type": "inputText",
                    "text": "Host tool execution failed."}],
                    "success": False}
        if validator_binding is not None and not isinstance(result, Mapping):
            result = {"status": "failed", "failure_class": "validator_result_not_object",
                      "validator": validator_binding.validator_id}
        safe_result = _safe_value(result)
        if not isinstance(safe_result, Mapping):
            safe_result = {"result": safe_result}
        result_for_host = dict(safe_result)
        result_class = "success"
        validator_passed = False
        if validator_binding is not None:
            try:
                result_schema = validator_binding.result_schema_value()
                jsonschema.validators.validator_for(result_schema)(
                    result_schema).validate(result_for_host)
                validator_passed = evaluate_validator(
                    "completion-boundary-v1", result_for_host).passed
            except (jsonschema.ValidationError, ValueError, TypeError):
                validator_passed = False
            result_class = "success" if validator_passed else "failure"

        digest = hashlib.sha256(_json_bytes(result_for_host)).hexdigest()
        tool_evidence_ref = self._host.after_tool(
            tool_type=binding.descriptor.tool_type, result_class=result_class,
            subject_ref=subject_ref, invocation_id=call_id,
            action_request_id=action_request_id, result_digest=digest,
            actual_result=(result_for_host if validator_binding is not None else
                           {"success": True, "result_sha256": digest}),
            covered_boundary_ids=(validator_binding.covered_boundary_ids
                                  if validator_binding is not None else ()))
        evidence_ref = tool_evidence_ref
        self._host.complete_action()
        validator_evidence_ref = ""
        if validator_binding is not None and validator_passed:
            claim_digest = hashlib.sha256(_json_bytes({
                "validator_id": validator_binding.validator_id,
                "arguments": validator_binding.arguments_value(),
                "result_schema": validator_binding.result_schema_value(),
            })).hexdigest()
            try:
                validator_evidence_ref = self._host.after_validator(
                    validator_id="completion-boundary-v1", invocation_id=call_id,
                    result=result_for_host,
                    subject_ref=self.request.subject_ref or self.request.task_id,
                    claim_digest=claim_digest,
                    covered_boundary_ids=validator_binding.covered_boundary_ids)
            except Exception:
                self._cognitive_fault = "validator_attestation_failed"
                return {"contentItems": [{"type": "inputText",
                    "text": "Validator ran, but the host could not attest its result."}],
                    "success": False}
        if validator_evidence_ref:
            evidence_ref = validator_evidence_ref
        if self.cognitive_host is not None:
            try:
                after = await self._cognitive_call(
                    "after_action", action=action,
                    binding=binding.action_binding_id, invocation_id=call_id,
                    action_request_id=action_request_id,
                    actual_result=result_for_host, result_digest=digest,
                    evidence_ref=str(evidence_ref))
                self._require_boundary_ready("post-action", after)
            except Exception as exc:
                self._cognitive_fault = "post_action_boundary_failed"
                self._cognitive_fault_detail = _safe_text(
                    f"{type(exc).__name__}: {exc}", 256)
                return {"contentItems": [{"type": "inputText",
                                          "text": "Action ran, but host control paused the task after it."}],
                        "success": False}
            if validator_binding is not None:
                try:
                    observed = await self._cognitive_call(
                        "observe_validator", validator_id=validator_binding.validator_id,
                        result=result_for_host,
                        criterion_evidence_refs={criterion_id: (
                            (validator_evidence_ref,) if validator_evidence_ref else ())
                            for criterion_id in validator_binding.criterion_ids})
                    self._require_boundary_ready("dynamic validator observation", observed)
                except Exception:
                    self._cognitive_fault = "dynamic_validator_observation_failed"
                    return {"contentItems": [{"type": "inputText",
                        "text": "Validator ran, but host control paused after its result."}],
                        "success": False}
        if validator_binding is not None:
            self._remember_validation_epoch(
                validator_binding, artifact_digest_before=artifact_digest_before,
                invocation_id=call_id, result=result_for_host,
                result_digest=digest, evidence_ref=str(tool_evidence_ref or ""),
                passed=validator_passed)
        public_result = _public_json(result_for_host)
        return {"contentItems": [{"type": "inputText", "text": public_result}],
                # The host tool call succeeded when the executor returned a
                # typed result; the result may still say that a criterion fails.
                "success": True}
    async def _approve(self, payload: Mapping[str, Any]) -> bool:
        if self.approval_handler is None:
            return False
        decision = self.approval_handler(_safe_value(payload))
        if inspect.isawaitable(decision):
            decision = await decision
        return decision is True or decision == "accept"
    async def _handle_command_approval(self, params: Mapping[str, Any]) -> dict[str, Any]:
        item_id = params.get("itemId")
        command = params.get("command")
        cwd = params.get("cwd")
        if (not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 256
                or not isinstance(command, str) or not command.strip() or len(command) > 8192
                or (cwd is not None and (not isinstance(cwd, str) or len(cwd) > 2048))):
            return {"decision": "decline"}
        if item_id in self._pending_native_actions:
            return {"decision": "accept"}
        action = {"tool_binding_id": "shell",
                  "arguments": {"command": command, "cwd": str(cwd or self.cwd)},
                  "subject_ref": self.request.subject_ref}
        prepared = self._host.prepare_tool(action)
        approved = (prepared.decision == GateDecision.ALLOW and await self._approve({
            "kind": "command", "item_id": item_id,
            "command": _safe_text(command, 2048), "cwd": str(cwd or self.cwd),
            "reason": params.get("reason"),
        }))
        if not approved:
            self._host.observation("codex.command.blocked", self.request.subject_ref,
                                   details={"item_id": item_id,
                                            "decision": prepared.decision.value})
            return {"decision": "decline"}
        self._pending_native_actions[item_id] = [{
            "action_request_id": prepared.action_request_id,
            "binding_id": "shell", "invocation_id": f"codex-command:{item_id}",
        }]
        return {"decision": "accept"}
    async def _handle_file_change_approval(self,
                                           params: Mapping[str, Any]) -> dict[str, Any]:
        item_id = params.get("itemId")
        change_item = self._file_change_items.get(str(item_id))
        changes = change_item.get("changes") if isinstance(change_item, Mapping) else None
        if (not isinstance(item_id, str) or not isinstance(changes, list)
                or not item_id.strip() or len(item_id) > 256
                or not changes or len(changes) > 64):
            return {"decision": "decline"}
        if item_id in self._pending_native_actions:
            return {"decision": "accept"}
        prepared_actions: list[dict[str, Any]] = []
        for change in changes:
            if (not isinstance(change, Mapping)
                    or not isinstance(change.get("path"), str)
                    or not change.get("path") or len(change["path"]) > 1024):
                self._close_prepared_native_actions(item_id, prepared_actions, "declined")
                return {"decision": "decline"}
            diff = str(change.get("diff") or "")
            if len(diff.encode("utf-8")) > 256 * 1024:
                self._close_prepared_native_actions(item_id, prepared_actions, "declined")
                return {"decision": "decline"}
            action = {"tool_binding_id": "write",
                      "arguments": {"path": change["path"],
                                     "operation": str(change.get("kind") or "modify"),
                                     "diff_sha256": hashlib.sha256(
                                         diff.encode("utf-8")).hexdigest()},
                      "subject_ref": self.request.subject_ref}
            preparation = self._host.prepare_tool(action)
            if preparation.decision != GateDecision.ALLOW:
                self._close_prepared_native_actions(item_id, prepared_actions, "declined")
                self._host.observation(
                    "codex.file_change.blocked", self.request.subject_ref,
                    details={"item_id": item_id,
                             "decision": preparation.decision.value})
                return {"decision": "decline"}
            prepared_actions.append({
                "action_request_id": preparation.action_request_id,
                "binding_id": "write",
                "invocation_id": f"codex-file-change:{item_id}:{len(prepared_actions)}",
                "path": str(change["path"]), "diff_sha256": hashlib.sha256(
                    diff.encode("utf-8")).hexdigest(),
            })
        approved = await self._approve({
            "kind": "file_change", "item_id": item_id,
            "changes": [{"path": _safe_text(str(change.get("path") or ""), 512),
                         "kind": _safe_text(str(change.get("kind") or "modify"), 64),
                         "diff": _safe_text(str(change.get("diff") or ""), 4096)}
                        for change in changes],
        })
        if not approved:
            self._close_prepared_native_actions(item_id, prepared_actions, "declined")
            return {"decision": "decline"}
        self._pending_native_actions[item_id] = prepared_actions
        return {"decision": "accept"}
    def _close_prepared_native_actions(self, item_id: str,
                                       actions: Sequence[Mapping[str, Any]],
                                       result_class: str) -> None:
        for action in actions:
            self._host.after_tool(
                tool_type=str(action["binding_id"]), result_class=result_class,
                subject_ref=self.request.subject_ref,
                invocation_id=str(action["invocation_id"]),
                action_request_id=str(action["action_request_id"]),
                result_digest=hashlib.sha256(result_class.encode()).hexdigest(),
                actual_result={"executed": False, "item_id": item_id})
            self._host.complete_action()
    def _settle_pending_native_actions(self, result_class: str) -> None:
        for item_id, actions in tuple(self._pending_native_actions.items()):
            self._pending_native_actions.pop(item_id, None)
            self._close_prepared_native_actions(item_id, actions, result_class)
    def handle_notification(self, method: str, params: Mapping[str, Any]) -> None:
        self._emit_notification(method, params)
        if method == "turn/started":
            turn = params.get("turn")
            if (self._active_turn_id is None
                    and isinstance(turn, Mapping)
                    and isinstance(turn.get("id"), str)):
                self._active_turn_id = str(turn["id"])
            return
        if method == "item/started":
            item = params.get("item")
            if isinstance(item, Mapping) and item.get("type") != "userMessage":
                self._model_execution_observed = True
                self._advance_phase(CodexExecutionPhase.MODEL_ACTIVITY_OBSERVED)
            if isinstance(item, Mapping) and item.get("type") == "fileChange":
                item_id = item.get("id")
                if (isinstance(item_id, str) and len(item_id) <= 256
                        and (item_id in self._file_change_items
                             or len(self._file_change_items) < 128)):
                    self._file_change_items[item_id] = dict(item)
            return
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, Mapping):
                item_id = item.get("id")
                item_type = item.get("type")
                if item_type == "agentMessage" and isinstance(item.get("text"), str):
                    self._last_assistant_message = _safe_text(str(item["text"]), 16_000)
                    self._model_execution_observed = True
                    self._advance_phase(CodexExecutionPhase.MODEL_ACTIVITY_OBSERVED)
                if isinstance(item_id, str):
                    self._complete_native_item(item_id, item)
                    self._file_change_items.pop(item_id, None)
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                self._model_execution_observed = True
                self._advance_phase(CodexExecutionPhase.MODEL_ACTIVITY_OBSERVED)
                self._last_assistant_message = _safe_text(
                    self._last_assistant_message + delta, 16_000)
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, Mapping):
                return
            turn_id = turn.get("id")
            status = turn.get("status")
            if not isinstance(turn_id, str) or status not in {
                    "completed", "failed", "interrupted"}:
                return
            self._last_turn_status = str(status)
            result = self._turn_result(turn)
            if turn_id == self._active_turn_id and self._active_attempt_ref:
                self._finish_turn_notification(turn, result)
            else:
                # App Server may publish completion before the turn/start RPC
                # response. The host creates the CFW attempt once that response
                # supplies its stable turn ID, then consumes this cached event.
                self._early_completion = result
                self._early_turn_event = dict(turn)
            return
        if method in {"thread/status/changed", "thread/closed"}:
            self._last_turn_status = self._last_turn_status or "idle"
    def _emit_notification(self, method: str, params: Mapping[str, Any]) -> None:
        if self.event_handler is None:
            return
        try:
            result = self.event_handler(method, _safe_value(params))
            if inspect.isawaitable(result):
                task = asyncio.create_task(result)
                task.add_done_callback(self._consume_event_task)
        except Exception:
            # Observer callbacks cannot change task or action authority.
            return
    def _consume_event_task(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()
    def _finish_turn_notification(self, turn: Mapping[str, Any],
                                   result: Mapping[str, Any]) -> None:
        try:
            if turn.get("status") in {"failed", "interrupted"}:
                self._record_turn_failure(turn)
        except Exception as exc:
            if self._turn_completion is not None and not self._turn_completion.done():
                self._turn_completion.set_exception(exc)
        else:
            if self._turn_completion is not None and not self._turn_completion.done():
                self._turn_completion.set_result(dict(result))
        finally:
            self._active_turn_id = None
            self._active_attempt_ref = None
    def _complete_native_item(self, item_id: str, item: Mapping[str, Any]) -> None:
        actions = self._pending_native_actions.pop(item_id, ())
        if not actions:
            return
        status = str(item.get("status") or "unknown")
        result_class = "success" if status == "completed" else (
            "declined" if status == "declined" else "failure")
        safe_result = _safe_value(item)
        digest = hashlib.sha256(_json_bytes(safe_result)).hexdigest()
        for action in actions:
            self._host.after_tool(
                tool_type=str(action["binding_id"]), result_class=result_class,
                subject_ref=self.request.subject_ref,
                invocation_id=str(action["invocation_id"]),
                action_request_id=str(action["action_request_id"]),
                result_digest=digest,
                actual_result={"status": status, "result_sha256": digest})
            self._host.complete_action()
    def _record_turn_failure(self, turn: Mapping[str, Any]) -> None:
        if self._host is None or not self._active_attempt_ref:
            return
        error = turn.get("error") if isinstance(turn.get("error"), Mapping) else {}
        evidence_ref = self._host.observation(
            "codex.turn.failure", self.request.subject_ref,
            details={"turn_id": str(turn.get("id") or "")[:256],
                     "status": str(turn.get("status") or "failed"),
                     "error": _safe_error(error)})
        self._host.record_attempt_failure(
            attempt_ref=self._active_attempt_ref, evidence_ref=evidence_ref)
    def _turn_result(self, turn: Mapping[str, Any]) -> dict[str, Any]:
        error = turn.get("error") if isinstance(turn.get("error"), Mapping) else {}
        return {
            "thread_id": self._thread_id,
            "turn_id": str(turn.get("id") or ""),
            "status": str(turn.get("status") or "unknown"),
            "assistant_message": self._last_assistant_message,
            "error": _safe_error(error),
        }
