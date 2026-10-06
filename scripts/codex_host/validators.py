"""Typed host validator bindings and evidence-bound completion."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .protocol import (
    _VALIDATOR_ID,
    MAX_TOOL_SCHEMA_BYTES,
)


@dataclass(frozen=True)
class CodexValidatorBinding:
    """Host-owned validator execution bound to criteria and a registered action.

    The validator receives fixed host-authored arguments. Its output schema
    defines the expected result semantics; the executor itself remains behind
    the CFW action gate and, when attached, the DP action boundary.
    """

    validator_id: str
    action_binding_id: str
    arguments: Mapping[str, Any]
    criterion_ids: Sequence[str]
    result_schema: Mapping[str, Any]
    covered_boundary_ids: Sequence[str] = ()
    _arguments_json: str = field(init=False, repr=False, compare=False)
    _result_schema_json: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.validator_id, str) or not _VALIDATOR_ID.fullmatch(
                self.validator_id):
            raise ValueError("validator_id must be a bounded host-owned identifier")
        if (not isinstance(self.action_binding_id, str)
                or not self.action_binding_id or len(self.action_binding_id) > 128):
            raise ValueError("validator action_binding_id must be bounded non-empty text")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("validator arguments must be a host-authored object")
        try:
            arguments_json = json.dumps(dict(self.arguments), sort_keys=True,
                                        separators=(",", ":"), ensure_ascii=False,
                                        allow_nan=False)
            arguments = json.loads(arguments_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("validator arguments must contain only JSON values") from exc
        if len(arguments_json.encode("utf-8")) > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("validator arguments exceed the size bound")
        if not isinstance(self.criterion_ids, (tuple, list)):
            raise TypeError("validator criterion_ids must be a sequence of IDs")
        criterion_ids = tuple(self.criterion_ids)
        if (not criterion_ids or len(criterion_ids) > 64
                or any(not isinstance(item, str) or not item.strip() or len(item) > 128
                       for item in criterion_ids)
                or len(set(criterion_ids)) != len(criterion_ids)):
            raise ValueError("validator must bind one or more unique bounded criteria")
        if not isinstance(self.covered_boundary_ids, (tuple, list)):
            raise TypeError("validator covered_boundary_ids must be a sequence of IDs")
        covered = tuple(self.covered_boundary_ids)
        if (len(covered) > 256
                or any(not isinstance(item, str) or not item.strip() or len(item) > 128
                       for item in covered)
                or len(set(covered)) != len(covered)):
            raise ValueError("validator boundary IDs must be unique bounded identifiers")
        if not isinstance(self.result_schema, Mapping):
            raise TypeError("validator result_schema must be a JSON Schema object")
        try:
            schema_json = json.dumps(dict(self.result_schema), sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False)
            schema = json.loads(schema_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("validator result_schema must contain only JSON values") from exc
        if schema.get("type") != "object":
            raise ValueError("validator result_schema must describe an object")
        if len(schema_json.encode("utf-8")) > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("validator result_schema exceeds the size bound")
        try:
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise ValueError("validator result_schema is invalid JSON Schema") from exc
        object.__setattr__(self, "arguments", arguments)
        object.__setattr__(self, "criterion_ids", criterion_ids)
        object.__setattr__(self, "covered_boundary_ids", covered)
        object.__setattr__(self, "_arguments_json", arguments_json)
        object.__setattr__(self, "_result_schema_json", schema_json)

    def arguments_value(self) -> dict[str, Any]:
        return json.loads(self._arguments_json)

    def result_schema_value(self) -> dict[str, Any]:
        return json.loads(self._result_schema_json)
