"""Validator execution and evidence-bound completion gate."""
from __future__ import annotations

import hashlib
import inspect
import json
import marshal
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from runtime_contract.validators import evaluate_validator
from runtime_session.lifecycle import GateDecision

from .protocol import (
    MAX_TOOL_OUTPUT_BYTES,
    CodexExecutionPhase,
    _goal_contract_data,
    _json_bytes,
    _safe_error,
    _safe_value,
)
from .validators import CodexValidatorBinding

# External host and executor boundaries are deliberately fail-closed on any error.
# ruff: noqa: BLE001


@dataclass(frozen=True)
class ValidationEpoch:
    artifact_digest: str
    validator_contract_hash: str
    validator_id: str
    criterion_ids: tuple[str, ...]
    evidence_ref: str
    result_digest: str
    invocation_id: str
    passed: bool
    result_json: str


class CodexCompletionMixin:
    _ARTIFACT_IGNORED_DIRS = frozenset({
        ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".tox", ".venv", "venv", "node_modules",
    })
    _ARTIFACT_MAX_FILES = 4096
    _ARTIFACT_MAX_BYTES = 128 * 1024 * 1024

    @staticmethod
    def _reuse_basis_digest(*, artifact_digest: str,
                            validator_contract_hash: str,
                            criterion_ids: tuple[str, ...],
                            evidence_ref: str,
                            result_digest: str) -> str:
        basis = {
            "artifact_digest": artifact_digest,
            "validator_contract_hash": validator_contract_hash,
            "criterion_ids": list(criterion_ids),
            "evidence_ref": evidence_ref,
            "result_digest": result_digest,
        }
        return hashlib.sha256(_json_bytes(basis)).hexdigest()

    def _validation_artifact_digest(self) -> str | None:
        """Hash the task workspace while excluding repository and runtime caches."""
        root = Path(self.cwd).resolve()
        if not root.is_dir():
            return None
        digest = hashlib.sha256()
        file_count = 0
        total_bytes = 0
        try:
            for raw_dir, dirnames, filenames in os.walk(root, followlinks=False):
                relative_dir = Path(raw_dir).relative_to(root)
                dirnames[:] = sorted(
                    name for name in dirnames
                    if name not in self._ARTIFACT_IGNORED_DIRS
                    and not (relative_dir == Path("xdg-runtime")
                             and name == "cognitiveframeworks"))
                for name in sorted(filenames):
                    path = Path(raw_dir) / name
                    if path.is_symlink():
                        return None
                    before = path.stat(follow_symlinks=False)
                    if not stat.S_ISREG(before.st_mode):
                        continue
                    file_count += 1
                    total_bytes += before.st_size
                    if (file_count > self._ARTIFACT_MAX_FILES
                            or total_bytes > self._ARTIFACT_MAX_BYTES):
                        return None
                    content = path.read_bytes()
                    after = path.stat(follow_symlinks=False)
                    if (before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns
                            or len(content) != after.st_size):
                        return None
                    digest.update(path.relative_to(root).as_posix().encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(str(stat.S_IMODE(after.st_mode)).encode("ascii"))
                    digest.update(b"\0")
                    digest.update(content)
                    digest.update(b"\0")
        except OSError:
            return None
        return digest.hexdigest()

    def _validator_contract_hash(self, binding: CodexValidatorBinding) -> str | None:
        action = self._action_bindings_by_id.get(binding.action_binding_id)
        executor = getattr(action, "executor", None)
        code = getattr(executor, "__code__", None)
        if action is None or code is None:
            return None
        try:
            executor_code_hash = hashlib.sha256(marshal.dumps(code)).hexdigest()
            goal_hash = str(self._task_handle.bundle.pinned.get(
                "goal_contract_hash") or "")
            contract = {
                "goal_contract_hash": goal_hash,
                "validator_id": binding.validator_id,
                "action_binding_id": binding.action_binding_id,
                "arguments": binding.arguments_value(),
                "result_schema": binding.result_schema_value(),
                "criterion_ids": list(binding.criterion_ids),
                "covered_boundary_ids": list(binding.covered_boundary_ids),
                "action_name": action.name,
                "action_schema": json.loads(action._schema_json),
                "action_tool_type": action.descriptor.tool_type,
                "action_goal_relation": action.descriptor.goal_relation,
                "executor_module": getattr(executor, "__module__", ""),
                "executor_qualname": getattr(executor, "__qualname__", ""),
                "executor_code_hash": executor_code_hash,
            }
            return hashlib.sha256(_json_bytes(contract)).hexdigest()
        except (AttributeError, TypeError, ValueError, OSError):
            return None

    def _current_validation_evidence(self, evidence_ref: str) -> bool:
        check = getattr(self._host, "evidence_is_current", None)
        if not callable(check):
            return False
        try:
            return check(evidence_ref) is True
        except Exception:
            return False

    def _reusable_validation_epoch(
            self, binding: CodexValidatorBinding,
            artifact_digest: str | None) -> dict[str, Any] | None:
        if artifact_digest is None:
            return None
        epoch = self._validation_epochs.get(binding.validator_id)
        contract_hash = self._validator_contract_hash(binding)
        if (not isinstance(epoch, ValidationEpoch) or contract_hash is None
                or epoch.artifact_digest != artifact_digest
                or epoch.validator_contract_hash != contract_hash
                or epoch.criterion_ids != tuple(binding.criterion_ids)
                or not self._current_validation_evidence(epoch.evidence_ref)):
            return None
        try:
            result = json.loads(epoch.result_json)
            if (hashlib.sha256(_json_bytes(result)).hexdigest() != epoch.result_digest
                    or not isinstance(result, Mapping)):
                return None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        schema_valid = True
        try:
            schema = binding.result_schema_value()
            jsonschema.validators.validator_for(schema)(schema).validate(result)
            schema_valid = evaluate_validator(
                "completion-boundary-v1", result).passed
        except (TypeError, ValueError, jsonschema.ValidationError):
            schema_valid = False
        # A cached pass must still satisfy the current registered result contract.
        # Failed results can be reused only to preserve a known failure outcome.
        if epoch.passed and not schema_valid:
            return None
        return {
            "passed": epoch.passed,
            "validator_id": epoch.validator_id,
            "criterion_ids": epoch.criterion_ids,
            "invocation_id": epoch.invocation_id,
            "evidence_ref": epoch.evidence_ref,
            "result": dict(result),
            "result_digest": epoch.result_digest,
            "reuse_basis_digest": self._reuse_basis_digest(
                artifact_digest=epoch.artifact_digest,
                validator_contract_hash=epoch.validator_contract_hash,
                criterion_ids=epoch.criterion_ids,
                evidence_ref=epoch.evidence_ref,
                result_digest=epoch.result_digest),
            "reused": True,
            "channel": "host_completion_reused",
            "elapsed_ms": 0.0,
        }

    def _remember_validation_epoch(
            self, binding: CodexValidatorBinding, *,
            artifact_digest_before: str | None, invocation_id: str,
            result: Mapping[str, Any], result_digest: str,
            evidence_ref: str, passed: bool) -> None:
        if (artifact_digest_before is None or not evidence_ref
                or self._validation_artifact_digest() != artifact_digest_before
                or not self._current_validation_evidence(evidence_ref)):
            return
        contract_hash = self._validator_contract_hash(binding)
        if contract_hash is None:
            return
        result_json = _json_bytes(dict(result)).decode("utf-8")
        if hashlib.sha256(_json_bytes(dict(result))).hexdigest() != result_digest:
            return
        self._validation_epochs[binding.validator_id] = ValidationEpoch(
            artifact_digest=artifact_digest_before,
            validator_contract_hash=contract_hash,
            validator_id=binding.validator_id,
            criterion_ids=tuple(binding.criterion_ids),
            evidence_ref=evidence_ref,
            result_digest=result_digest,
            invocation_id=invocation_id,
            passed=bool(passed), result_json=result_json)

    @staticmethod
    def _validator_summaries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "validator_id": str(item.get("validator_id") or "")[:96],
            "criterion_ids": list(item.get("criterion_ids") or ())[:64],
            "passed": item.get("passed") is True,
            "reused": item.get("reused") is True,
            "evidence_ref": str(item.get("evidence_ref") or "")[:256],
            "result_digest": str(item.get("result_digest") or "")[:64],
            "reuse_basis_digest": str(item.get("reuse_basis_digest") or "")[:64],
        } for item in results[:64]]

    async def _run_validator(self, binding: CodexValidatorBinding) -> dict[str, Any]:
        """Execute one fixed host validator through the same action boundary."""
        self._validator_activity_observed = True
        self._advance_phase(CodexExecutionPhase.VALIDATION_OBSERVED)
        artifact_digest_before = self._validation_artifact_digest()
        reusable = self._reusable_validation_epoch(binding, artifact_digest_before)
        if reusable is not None:
            return reusable
        action_binding = self._action_bindings_by_id[binding.action_binding_id]
        invocation_id = "codex-validator:" + hashlib.sha256(
            f"{self.request.task_id}:{binding.validator_id}:"
            f"{secrets.token_urlsafe(24)}".encode()).hexdigest()
        arguments = binding.arguments_value()
        subject_ref = self.request.subject_ref or self.request.task_id
        action = {"tool_binding_id": binding.action_binding_id,
                  "arguments": arguments, "subject_ref": subject_ref}
        if self.cognitive_host is None:
            prepared = self._host.prepare_tool(action)
            if prepared.decision != GateDecision.ALLOW:
                return {"passed": False, "blocked": True,
                        "decision": prepared.decision.value,
                        "validator_id": binding.validator_id}
            action_request_id = prepared.action_request_id
            if not isinstance(action_request_id, str) or not action_request_id:
                self._cognitive_fault = "validator_action_correlation_missing"
                return {"passed": False, "blocked": True,
                        "decision": "host_correlation_missing",
                        "validator_id": binding.validator_id}
        else:
            try:
                prepared = await self._cognitive_call(
                    "before_action", action=action,
                    binding=binding.action_binding_id,
                    invocation_id=invocation_id)
            except Exception:
                self._cognitive_fault = "validator_pre_action_boundary_failed"
                return {"passed": False, "blocked": True,
                        "decision": "host_boundary_failed",
                        "validator_id": binding.validator_id}
            if (not isinstance(prepared, Mapping)
                    or prepared.get("action_authorized") is not True
                    or not isinstance(prepared.get("action_request_id"), str)
                    or not prepared.get("action_request_id")):
                return {"passed": False, "blocked": True,
                        "decision": "blocked",
                        "validator_id": binding.validator_id}
            action_request_id = str(prepared["action_request_id"])

        result_class = "failure"
        safe_result: dict[str, Any] = {
            "status": "failed", "failure_class": "validator_execution_error"}
        try:
            raw_result = action_binding.executor(arguments)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
            if not isinstance(raw_result, Mapping):
                raise TypeError("validator executor must return a typed object")
            candidate = _safe_value(dict(raw_result))
            if (not isinstance(candidate, Mapping)
                    or len(_json_bytes(candidate)) > MAX_TOOL_OUTPUT_BYTES):
                raise ValueError("validator result exceeds the bounded object limit")
            safe_result = dict(candidate)
            schema = binding.result_schema_value()
            jsonschema.validators.validator_for(schema)(schema).validate(safe_result)
            if not evaluate_validator("completion-boundary-v1", safe_result).passed:
                raise ValueError("validator result did not pass the registered completion check")
            result_class = "success"
        except Exception as exc:
            if (isinstance(exc, (TypeError, ValueError, jsonschema.ValidationError))
                    and "raw_result" in locals()
                    and isinstance(raw_result, Mapping)):
                candidate = _safe_value(dict(raw_result))
                if (isinstance(candidate, Mapping)
                        and len(_json_bytes(candidate)) <= MAX_TOOL_OUTPUT_BYTES):
                    safe_result = dict(candidate)
            failure_class = ("validator_result_invalid" if isinstance(
                exc, (TypeError, ValueError, jsonschema.ValidationError))
                else "validator_execution_error")
            if not isinstance(safe_result.get("status"), str):
                safe_result = {"status": "failed", "failure_class": failure_class}

        result_digest = hashlib.sha256(_json_bytes(safe_result)).hexdigest()
        try:
            evidence_ref = self._host.after_tool(
                tool_type=action_binding.descriptor.tool_type,
                result_class=result_class, subject_ref=subject_ref,
                invocation_id=invocation_id, action_request_id=action_request_id,
                result_digest=result_digest, actual_result=safe_result,
                covered_boundary_ids=binding.covered_boundary_ids)
            self._host.complete_action()
        except Exception:
            self._cognitive_fault = "validator_evidence_failed"
            return {"passed": False, "blocked": True,
                    "decision": "host_evidence_failed",
                    "validator_id": binding.validator_id}

        if self.cognitive_host is not None:
            try:
                after = await self._cognitive_call(
                    "after_action", action=action,
                    binding=binding.action_binding_id,
                    invocation_id=invocation_id,
                    action_request_id=action_request_id,
                    actual_result=safe_result, result_digest=result_digest,
                    evidence_ref=str(evidence_ref or ""))
                self._require_boundary_ready("validator post-action", after)
            except Exception:
                self._cognitive_fault = "validator_post_action_boundary_failed"
                return {"passed": False, "blocked": True,
                        "decision": "host_boundary_failed",
                        "validator_id": binding.validator_id}
        self._remember_validation_epoch(
            binding, artifact_digest_before=artifact_digest_before,
            invocation_id=invocation_id, result=safe_result,
            result_digest=result_digest, evidence_ref=str(evidence_ref or ""),
            passed=result_class == "success")
        validator_contract_hash = self._validator_contract_hash(binding) or ""
        reuse_basis_digest = (self._reuse_basis_digest(
            artifact_digest=artifact_digest_before,
            validator_contract_hash=validator_contract_hash,
            criterion_ids=tuple(binding.criterion_ids),
            evidence_ref=str(evidence_ref), result_digest=result_digest)
            if artifact_digest_before and validator_contract_hash and evidence_ref else "")
        return {"passed": result_class == "success",
                "validator_id": binding.validator_id,
                "criterion_ids": binding.criterion_ids,
                "invocation_id": invocation_id,
                "evidence_ref": str(evidence_ref or ""),
                "result_digest": result_digest,
                "reuse_basis_digest": reuse_basis_digest,
                "result": safe_result}
    async def validate_completion(self, *,
                                  subject_ref: str | None = None) -> dict[str, Any]:
        if self._active_turn_id is not None or self._turn_completion is not None:
            return {"status": "incomplete", "decision": "turn_in_progress",
                    "task_id": self.request.task_id}
        async with self._completion_lock:
            if self._active_turn_id is not None or self._turn_completion is not None:
                return {"status": "incomplete", "decision": "turn_in_progress",
                        "task_id": self.request.task_id}
            async with self._action_lock:
                return await self._validate_completion_locked(subject_ref=subject_ref)
    async def _validate_completion_locked(self, *,
                                          subject_ref: str | None = None
                                          ) -> dict[str, Any]:
        """Run the host validator set and request CFW's evidence-bound permit."""
        self._completion_checked = True
        self._advance_phase(CodexExecutionPhase.COMPLETION_CHECKED)
        if self._host is None:
            raise RuntimeError("adapter must be open before completion validation")
        if self._cognitive_fault is not None:
            return {"status": "incomplete", "decision": "reconciliation_required",
                    "task_id": self.request.task_id}
        if not self.validator_bindings:
            return {"status": "incomplete", "decision": "host_validators_missing",
                    "task_id": self.request.task_id}

        results: list[dict[str, Any]] = []
        criterion_evidence_refs: dict[str, list[str]] = {}
        for validator_binding in self.validator_bindings:
            run = await self._run_validator(validator_binding)
            results.append(run)
            if run.get("blocked"):
                return {"status": "incomplete", "decision": run.get("decision"),
                        "task_id": self.request.task_id,
                        "validator_id": validator_binding.validator_id,
                        "validators": self._validator_summaries(results)}
            evidence_ref = run.get("evidence_ref")
            if isinstance(evidence_ref, str) and evidence_ref:
                for criterion_id in validator_binding.criterion_ids:
                    criterion_evidence_refs.setdefault(criterion_id, []).append(evidence_ref)
            if self.cognitive_host is not None:
                try:
                    observed = await self._cognitive_call(
                        "observe_validator",
                        validator_id=validator_binding.validator_id,
                        result=run.get("result", {"status": "failed"}),
                        criterion_evidence_refs={criterion_id: tuple(
                            criterion_evidence_refs.get(criterion_id, ()))
                            for criterion_id in validator_binding.criterion_ids})
                    self._require_boundary_ready("validator observation", observed)
                except Exception:
                    self._cognitive_fault = "validator_observation_failed"
                    return {"status": "incomplete", "decision": "host_boundary_failed",
                            "task_id": self.request.task_id,
                            "validator_id": validator_binding.validator_id,
                            "validators": self._validator_summaries(results)}

        required_criteria = {
            str(item.get("criterion_id"))
            for item in _goal_contract_data(self.request).get("acceptance_criteria", ())
            if isinstance(item, Mapping) and item.get("required", True)}
        covered_criteria = set(criterion_evidence_refs)
        if (not required_criteria.issubset(covered_criteria)
                or any(not result.get("passed") for result in results)):
            return {"status": "incomplete", "decision": "validator_failed",
                    "task_id": self.request.task_id,
                    "required_criteria": len(required_criteria),
                    "validated_criteria": len(required_criteria & covered_criteria),
                    "validators": self._validator_summaries(results)}

        final_run = results[-1]
        final_result = final_run.get("result")
        if (not isinstance(final_result, Mapping)
                or not evaluate_validator("completion-boundary-v1",
                                          final_result).passed):
            return {"status": "incomplete", "decision": "completion_validator_failed",
                    "task_id": self.request.task_id,
                    "validator_id": final_run.get("validator_id"),
                    "validators": self._validator_summaries(results)}

        evidence = {key: tuple(refs) for key, refs in criterion_evidence_refs.items()}
        completion_summary = {
            "status": "passed",
            "validators": [{"validator_id": item["validator_id"],
                             "criterion_ids": list(item["criterion_ids"]),
                             "status": item["result"].get("status")}
                            for item in results],
        }
        if self.cognitive_host is not None:
            try:
                boundary = await self._cognitive_call(
                    "before_completion", validator_id="completion-boundary-v1",
                    result=completion_summary,
                    criterion_evidence_refs=evidence)
                self._require_boundary_ready("pre-completion", boundary)
                if isinstance(boundary, Mapping) and boundary.get("decision") in {
                        "block", "deny", "reject"}:
                    return {"status": "incomplete", "decision": "block",
                            "task_id": self.request.task_id,
                            "validators": self._validator_summaries(results)}
            except Exception:
                self._cognitive_fault = "completion_boundary_failed"
                return {"status": "incomplete", "decision": "host_boundary_failed",
                        "task_id": self.request.task_id,
                        "validators": self._validator_summaries(results)}

        try:
            decision = self._host.validate_completion_boundary(
                subject_ref=subject_ref or self.request.subject_ref or self.request.task_id,
                validator_id="completion-boundary-v1",
                invocation_id=str(final_run["invocation_id"]),
                result=dict(final_result), criterion_ids=tuple(evidence),
                criterion_evidence_refs=evidence)
        except Exception as exc:
            return {"status": "incomplete", "decision": "evidence_validation_failed",
                    "error_class": type(exc).__name__,
                    "task_id": self.request.task_id,
                    "validators": self._validator_summaries(results)}
        if decision != GateDecision.ALLOW:
            return {"status": "incomplete", "decision": decision.value,
                    "task_id": self.request.task_id,
                    "validators": self._validator_summaries(results)}
        try:
            finished = self._host.finish_task({
                "status": "completed", "completed": True,
                "codex_thread_id": self._thread_id,
                "validator_id": "completion-boundary-v1",
            })
        except Exception as exc:
            return {"status": "incomplete", "decision": "finalization_failed",
                    "error_class": type(exc).__name__,
                    "error_message": _safe_error({"message": str(exc)}).get(
                        "message"),
                    "task_id": self.request.task_id,
                    "validators": self._validator_summaries(results)}
        completed = bool(finished.get("completed", False))
        return {"status": "completed" if completed else "incomplete",
                "decision": "allow" if completed else "finalization_failed",
                "task_id": self.request.task_id,
                "thread_id": self._thread_id,
                "completed": completed,
                "validated_criteria": len(required_criteria),
                "validators": self._validator_summaries(results)}
