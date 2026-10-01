"""Load hashes and the question registry from the canonical vendored protocol."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


_SOURCE_SCHEMA = Path(__file__).resolve().parents[2] / "contracts" / "system-one-protocol.schema.json"
_PACKAGE_SCHEMA = Path(__file__).resolve().parent / "contracts" / "system-one-protocol.schema.json"
SCHEMA_PATH = _SOURCE_SCHEMA if _SOURCE_SCHEMA.is_file() else _PACKAGE_SCHEMA


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def load_protocol() -> Mapping[str, Any]:
    value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or not isinstance(value.get("$defs"), Mapping):
        raise RuntimeError("canonical System-1 protocol schema is malformed")
    return value


PROTOCOL_SCHEMA = load_protocol()
PROTOCOL_VERSION = str(PROTOCOL_SCHEMA["protocol_version"])
PROTOCOL_HASH = hashlib.sha256(_canonical(PROTOCOL_SCHEMA)).hexdigest()
QUESTION_REGISTRY = PROTOCOL_SCHEMA["x-question-registry"]
QUESTION_REGISTRY_HASH = hashlib.sha256(_canonical(QUESTION_REGISTRY)).hexdigest()
STATE_PROJECTION_SCHEMA_HASH = hashlib.sha256(
    _canonical(PROTOCOL_SCHEMA["$defs"]["SemanticState"])).hexdigest()


__all__ = ["PROTOCOL_VERSION", "PROTOCOL_HASH", "QUESTION_REGISTRY",
           "QUESTION_REGISTRY_HASH", "STATE_PROJECTION_SCHEMA_HASH", "PROTOCOL_SCHEMA"]
