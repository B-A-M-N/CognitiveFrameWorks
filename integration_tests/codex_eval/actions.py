"""Host-owned dynamic action bindings for the coding fixture."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from codex_app_server_adapter import CodexActionBinding
from runtime_contract.actions import ActionDescriptor

from .fixture import _source_digest, _workspace_path
from .validators import VALIDATOR_FILES, _run_validator


def _make_actions(workspace: Path, *, validation_runs: list[dict[str, Any]],
                  run_channel: dict[str, str]) -> tuple[CodexActionBinding, ...]:
    def read(args: Mapping[str, Any]) -> dict[str, Any]:
        target = _workspace_path(workspace, str(args["path"]))
        if not target.is_file():
            raise FileNotFoundError("workspace file does not exist")
        data = target.read_text(encoding="utf-8")
        if len(data.encode("utf-8")) > 64 * 1024:
            raise ValueError("workspace read exceeds limit")
        return {"path": target.relative_to(workspace).as_posix(),
                "absolute_path": str(target), "content": data}

    def write(args: Mapping[str, Any]) -> dict[str, Any]:
        target = _workspace_path(workspace, str(args["path"]))
        content = str(args["content"])
        if len(content.encode("utf-8")) > 64 * 1024:
            raise ValueError("workspace write exceeds limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        prior = target.read_bytes() if target.is_file() else None
        encoded = content.encode("utf-8")
        target.write_bytes(encoded)
        return {"path": target.relative_to(workspace).as_posix(),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "changed": prior != encoded,
                "size_bytes": len(encoded)}

    def validate(args: Mapping[str, Any]) -> dict[str, Any]:
        result = _run_validator(workspace, str(args["validator"]))
        validation_runs.append({"channel": run_channel["value"], **result,
                                "source_sha256": _source_digest(workspace)})
        return result

    return (
        CodexActionBinding(
            name="cfw_read", description="Read a UTF-8 file in the task workspace.",
            input_schema={"type": "object", "properties": {
                "path": {"type": "string", "maxLength": 1024}},
                "required": ["path"], "additionalProperties": False},
            action_binding_id="read", descriptor=ActionDescriptor("read"), executor=read),
        CodexActionBinding(
            name="cfw_write", description=(
                "Write UTF-8 text to an absolute path inside the task workspace. "
                "For this task, only the src/ directory is in scope."),
            input_schema={"type": "object", "properties": {
                "path": {"type": "string", "maxLength": 2048},
                "content": {"type": "string", "maxLength": 64000}},
                "required": ["path", "content"], "additionalProperties": False},
            action_binding_id="write",
            descriptor=ActionDescriptor("write", mutability="write"), executor=write),
        CodexActionBinding(
            name="cfw_run_validator", description=(
                "Run one registered host test suite and return its measured result."),
            input_schema={"type": "object", "properties": {
                "validator": {"type": "string", "enum": list(VALIDATOR_FILES)}},
                "required": ["validator"], "additionalProperties": False},
            action_binding_id="host_validator",
            descriptor=ActionDescriptor("host_validator", mutability="read",
                goal_relation="required_verification",
                goal_intent="Run the registered test suite for its declared criterion"),
            executor=validate),
    )
