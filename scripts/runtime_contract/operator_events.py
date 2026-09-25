"""Canonical, sanitized operator event contract."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Tuple

OPERATOR_SCHEMA_VERSION = "1.0.0"
STATUSES = {"running", "waiting", "blocked", "recovering", "completed",
            "failed", "disconnected", "unknown"}
SEVERITIES = {"info", "notice", "warning", "critical"}
SOURCES = {"cfw", "csw", "dp"}


@dataclass(frozen=True)
class OperatorEvent:
    schema_version: str
    sequence: int
    timestamp: str
    namespace_id: str
    application_instance_id: str
    agent_instance_id: str | None
    session_id: str | None
    task_id: str | None
    interaction_id: str | None
    workflow_id: str | None
    event_type: str
    severity: str
    summary: str
    reason_code: str | None
    source_system: str
    provenance: str
    correlation_id: str
    evidence_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != OPERATOR_SCHEMA_VERSION:
            raise ValueError("unsupported operator event schema version")
        if self.sequence < 1 or self.source_system not in SOURCES:
            raise ValueError("operator event sequence/source is invalid")
        if self.severity not in SEVERITIES or not self.summary or not self.correlation_id:
            raise ValueError("operator event severity/summary/correlation is invalid")
        if self.provenance not in {"host_verified", "inferred", "synthetic", "unavailable"}:
            raise ValueError("operator event provenance is invalid")

    @classmethod
    def create(cls, *, sequence: int, namespace_id: str,
               application_instance_id: str, event_type: str, summary: str,
               source_system: str, correlation_id: str,
               agent_instance_id: str | None = None, session_id: str | None = None,
               task_id: str | None = None, interaction_id: str | None = None,
               workflow_id: str | None = None, severity: str = "info",
               reason_code: str | None = None,
               provenance: str = "host_verified",
               evidence_refs: Iterable[str] = (),
               timestamp: str | None = None) -> "OperatorEvent":
        return cls(
            OPERATOR_SCHEMA_VERSION, int(sequence), timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            namespace_id, application_instance_id, agent_instance_id, session_id, task_id,
            interaction_id, workflow_id, event_type, severity, summary[:512],
            reason_code, source_system, provenance, correlation_id, tuple(evidence_refs))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
