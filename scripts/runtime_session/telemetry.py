"""Host-owned telemetry adapter for the CFW runtime session."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from runtime_contract.paths import runtime_asset_root, runtime_state_dir
from runtime_contract.telemetry import runtime_event_schema_contract

ROOT = runtime_asset_root()
LOCAL_CONTRACT_ROOT = ROOT / "contracts"

OPTIONAL_EVENT_FIELDS = {
    "parent_event", "subject", "input_state", "output_state", "evidence_refs",
    "uncertainty", "authority_source", "scope", "supersedes", "invalidates", "payload",
    "interaction_id", "parent_session_id", "delegator_agent_id",
    "delegate_agent_id", "delegation_id", "role",
    "application_version", "provider_id", "model_id", "model_revision",
    "model_capability_hash", "harness_id", "harness_version",
    "goal_contract_hash",
}

_RUNTIME_EVENT_SCHEMA_VALIDATOR = None

class HostTelemetry:
    """Emit CFW-owned neutral runtime events to a per-task NDJSON store."""

    def __init__(self, task_id: str, store: Optional[Path] = None,
                 *, expected_schema_hash: Optional[str] = None,
                 expected_schema_version: Optional[str] = None,
                 namespace_id: str = "default", application_id: str = "unknown-application",
                 application_version: Optional[str] = None,
                 application_instance_id: Optional[str] = None,
                 provider_id: Optional[str] = None, model_id: Optional[str] = None,
                 model_revision: Optional[str] = None,
                 model_capability_hash: Optional[str] = None,
                 harness_id: Optional[str] = None, harness_version: Optional[str] = None,
                 agent_id: str = "agent", agent_instance_id: Optional[str] = None,
                 session_id: str = "session-unknown", attempt_id: str = "attempt-1",
                 segment_id: str = "segment-0", interaction_id: Optional[str] = None,
                 parent_session_id: Optional[str] = None,
                 delegator_agent_id: Optional[str] = None,
                 delegate_agent_id: Optional[str] = None,
                 delegation_id: Optional[str] = None, role: Optional[str] = None) -> None:
        self.task_id = task_id
        self.namespace_id = namespace_id
        self.application_id = application_id
        self.application_version = application_version
        self.application_instance_id = application_instance_id or application_id
        self.provider_id = provider_id
        self.model_id = model_id
        self.model_revision = model_revision
        self.model_capability_hash = model_capability_hash
        self.harness_id = harness_id
        self.harness_version = harness_version
        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id or agent_id
        self.session_id = session_id
        self.attempt_id = attempt_id
        self.segment_id = segment_id
        self.interaction_id = interaction_id
        self.parent_session_id = parent_session_id
        self.delegator_agent_id = delegator_agent_id
        self.delegate_agent_id = delegate_agent_id
        self.delegation_id = delegation_id
        self.role = role
        schema_path = LOCAL_CONTRACT_ROOT / "runtime-event.schema.json"
        schema, self.schema_hash, self.schema_version = runtime_event_schema_contract(
            schema_path)
        if expected_schema_hash and expected_schema_hash != self.schema_hash:
            raise ValueError(
                "pinned runtime-event schema hash does not match the CFW artifact")
        if expected_schema_version and expected_schema_version != self.schema_version:
            raise ValueError(
                "pinned runtime-event schema version does not match the CFW artifact")
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        root = runtime_state_dir().resolve()
        if store is None:
            self.store = (root / "telemetry" / f"{digest}.ndjson").resolve()
            if self.store.parent != (root / "telemetry").resolve():
                raise ValueError("telemetry path escapes runtime root")
        else:
            self.store = Path(store).resolve()
        self.store.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.store.parent, 0o700)
        except PermissionError:
            if self.store.parent == Path(tempfile.gettempdir()):
                pass
            else:
                raise

    def emit_event(self, *, event_id: Optional[str] = None, category: str, event_type: str,
                   agent_id: Optional[str] = None, actor_id: str = "runtime",
                   agent_instance_id: Optional[str] = None,
                   session_id: Optional[str] = None,
                   attempt_id: Optional[str] = None,
                   segment_id: Optional[str] = None,
                   behavioral_subject: Optional[str] = None,
                   interaction_id: Optional[str] = None,
                   parent_session_id: Optional[str] = None,
                   delegator_agent_id: Optional[str] = None,
                   delegate_agent_id: Optional[str] = None,
                   delegation_id: Optional[str] = None,
                   role: Optional[str] = None,
                   goal_contract_hash: Optional[str] = None,
                   subject: Optional[str] = None, evidence_refs=None,
                   invalidates: Optional[str] = None,
                   parent_event: Optional[str] = None,
                   input_state: Optional[str] = None,
                   output_state: Optional[str] = None,
                   uncertainty: Optional[float] = None,
                   authority_source: Optional[str] = None,
                   scope: Optional[str] = None,
                   supersedes: Optional[str] = None,
                   payload: Optional[Dict[str, Any]] = None) -> None:
        event = {
            "schema_version": self.schema_version,
            "event_id": event_id or f"evt-{uuid.uuid4()}",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "namespace_id": self.namespace_id,
            "application_id": self.application_id,
            "application_instance_id": self.application_instance_id,
            "task_id": self.task_id,
            "agent_id": agent_id or self.agent_id,
            "actor_id": actor_id,
            "agent_instance_id": agent_instance_id or self.agent_instance_id,
            "session_id": session_id or self.session_id,
            "attempt_id": attempt_id or self.attempt_id,
            "segment_id": segment_id or self.segment_id,
            "behavioral_subject": behavioral_subject or subject or f"agent:{agent_id or self.agent_id}",
            "category": category,
            "event_type": event_type,
        }
        for key, value in {
                "application_version": self.application_version,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "model_capability_hash": self.model_capability_hash,
                "harness_id": self.harness_id,
                "harness_version": self.harness_version}.items():
            if value is not None:
                event[key] = value
        if goal_contract_hash:
            event["goal_contract_hash"] = goal_contract_hash
        if subject:
            event["subject"] = subject
        if parent_event:
            event["parent_event"] = parent_event
        if input_state is not None:
            event["input_state"] = input_state
        if output_state is not None:
            event["output_state"] = output_state
        if evidence_refs:
            event["evidence_refs"] = list(evidence_refs)
        if uncertainty is not None:
            event["uncertainty"] = uncertainty
        if authority_source is not None:
            event["authority_source"] = authority_source
        if scope is not None:
            event["scope"] = scope
        if supersedes is not None:
            event["supersedes"] = supersedes
        if invalidates:
            event["invalidates"] = invalidates
        for key, value in {
                "interaction_id": interaction_id or self.interaction_id,
                "parent_session_id": parent_session_id or self.parent_session_id,
                "delegator_agent_id": delegator_agent_id or self.delegator_agent_id,
                "delegate_agent_id": delegate_agent_id or self.delegate_agent_id,
                "delegation_id": delegation_id or self.delegation_id,
                "role": role or self.role}.items():
            if value is not None:
                event[key] = value
        if payload is not None:
            event["payload"] = payload
        self._validate(event)
        with self.store.open("a", encoding="utf-8") as fh:
            os.chmod(self.store, 0o600)
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _validate(event: Dict[str, Any]) -> None:
        encoded = json.dumps(event, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("runtime telemetry event exceeds its 64 KiB bound")
        HostTelemetry._validate_metadata(event.get("payload") or {})
        global _RUNTIME_EVENT_SCHEMA_VALIDATOR
        try:
            import jsonschema
            from jsonschema import Draft202012Validator, FormatChecker
            schema_path = LOCAL_CONTRACT_ROOT / "runtime-event.schema.json"
            if _RUNTIME_EVENT_SCHEMA_VALIDATOR is None:
                schema, _schema_hash, _schema_version = runtime_event_schema_contract(
                    schema_path)
                _RUNTIME_EVENT_SCHEMA_VALIDATOR = Draft202012Validator(
                    schema, format_checker=FormatChecker())
            errors = sorted(_RUNTIME_EVENT_SCHEMA_VALIDATOR.iter_errors(event),
                            key=lambda error: list(error.path))
            if errors:
                raise ValueError(f"telemetry violates runtime-event.schema.json: {errors[0].message}")
        except ImportError as exc:
            raise ValueError("jsonschema is required for canonical telemetry validation") from exc

    @staticmethod
    def _validate_metadata(value: Any, *, depth: int = 0) -> None:
        if depth > 8:
            raise ValueError("runtime telemetry metadata exceeds its nesting bound")
        if value is None or isinstance(value, (bool, str)):
            if isinstance(value, str) and len(value) > 4096:
                raise ValueError("runtime telemetry metadata string exceeds its bound")
            return
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("runtime telemetry metadata number is not finite")
            if abs(value) > 1e12:
                raise ValueError("runtime telemetry metadata number exceeds its bound")
            return
        if isinstance(value, list):
            if len(value) > 256:
                raise ValueError("runtime telemetry metadata list exceeds its bound")
            for item in value:
                HostTelemetry._validate_metadata(item, depth=depth + 1)
            return
        if isinstance(value, dict):
            if len(value) > 64 or any(not isinstance(key, str) or len(key) > 256
                                      for key in value):
                raise ValueError("runtime telemetry metadata object exceeds its bound")
            for item in value.values():
                HostTelemetry._validate_metadata(item, depth=depth + 1)
            return
        raise ValueError("runtime telemetry metadata contains an unsupported value")

    @classmethod
    def validate_event(cls, event: Dict[str, Any], *, task_id: str,
                       expected_schema_hash: str,
                       expected_schema_version: str) -> None:
        """Validate even custom sinks against the pinned canonical schema."""
        allowed = {"schema_version", "event_id", "category", "event_type",
                   "namespace_id", "application_id", "application_instance_id",
                   "agent_id", "actor_id", "agent_instance_id", "session_id",
                   "attempt_id", "segment_id", "behavioral_subject", *OPTIONAL_EVENT_FIELDS}
        forbidden = set(event) - allowed
        if forbidden:
            raise ValueError(f"telemetry envelope has forbidden fields: {sorted(forbidden)}")
        schema_path = LOCAL_CONTRACT_ROOT / "runtime-event.schema.json"
        _schema, actual_hash, actual_version = runtime_event_schema_contract(schema_path)
        if actual_hash != expected_schema_hash or actual_version != expected_schema_version:
            raise ValueError("pinned runtime-event schema changed during the task")
        if event.get("schema_version") not in (None, actual_version):
            raise ValueError("telemetry event schema_version disagrees with the pinned schema")
        canonical = {
            "schema_version": actual_version,
            "event_id": event.get("event_id") or f"evt-{uuid.uuid4()}",
            "timestamp": event.get("timestamp") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "namespace_id": event.get("namespace_id", "default"),
            "application_id": event.get("application_id", "unknown-application"),
            "application_instance_id": event.get("application_instance_id", "unknown-application"),
            "task_id": task_id,
            "agent_id": event.get("agent_id", "runtime"),
            "actor_id": event.get("actor_id", "runtime"),
            "agent_instance_id": event.get("agent_instance_id", event.get("agent_id", "runtime")),
            "session_id": event.get("session_id", "session-unknown"),
            "attempt_id": event.get("attempt_id", "attempt-1"),
            "segment_id": event.get("segment_id", "segment-0"),
            "behavioral_subject": event.get("behavioral_subject") or event.get("subject") or "agent:runtime",
            "category": event.get("category", "observation"),
            "event_type": event.get("event_type", "runtime_event"),
        }
        for key in OPTIONAL_EVENT_FIELDS:
            if key in event and event[key] is not None:
                canonical[key] = event[key]
        cls._validate(canonical)

    def emit(self, event: Dict[str, Any]) -> None:
        if event.get("actor_id") in {"operator", "repository_owner", "service_owner"}:
            raise PermissionError("privileged actor identity is host-channel owned")
        allowed = {"schema_version", "event_id", "category", "event_type",
                   "namespace_id", "application_id", "application_instance_id",
                   "agent_id", "actor_id", "agent_instance_id", "session_id", "attempt_id",
                   "segment_id", "behavioral_subject", *OPTIONAL_EVENT_FIELDS}
        if set(event) - allowed:
            raise ValueError(f"telemetry envelope has forbidden fields: {sorted(set(event)-allowed)}")
        if event.get("schema_version") not in (None, self.schema_version):
            raise ValueError("telemetry event schema_version disagrees with the pinned schema")
        self.emit_event(
            event_id=event.get("event_id"),
            category=event.get("category", "observation"),
            event_type=event.get("event_type", "runtime_event"),
            agent_id=event.get("agent_id", self.agent_id),
            actor_id=event.get("actor_id", "runtime"),
            agent_instance_id=event.get("agent_instance_id", self.agent_instance_id),
            session_id=event.get("session_id", self.session_id),
            attempt_id=event.get("attempt_id", self.attempt_id),
            segment_id=event.get("segment_id", self.segment_id),
            behavioral_subject=event.get("behavioral_subject"),
            interaction_id=event.get("interaction_id", self.interaction_id),
            parent_session_id=event.get("parent_session_id", self.parent_session_id),
            delegator_agent_id=event.get("delegator_agent_id", self.delegator_agent_id),
            delegate_agent_id=event.get("delegate_agent_id", self.delegate_agent_id),
            delegation_id=event.get("delegation_id", self.delegation_id),
            role=event.get("role", self.role),
            goal_contract_hash=event.get("goal_contract_hash"),
            subject=event.get("subject"),
            evidence_refs=event.get("evidence_refs"),
            invalidates=event.get("invalidates"),
            parent_event=event.get("parent_event"),
            input_state=event.get("input_state"),
            output_state=event.get("output_state"),
            uncertainty=event.get("uncertainty"),
            authority_source=event.get("authority_source"),
            scope=event.get("scope"),
            supersedes=event.get("supersedes"),
            payload=event.get("payload"))

    def _emit_host_event(self, event: Dict[str, Any]) -> None:
        """Internal trusted adapter path for operator/channel attribution."""
        self.emit_event(
            event_id=event.get("event_id"), category=event.get("category", "observation"),
            event_type=event.get("event_type", "runtime_event"),
            agent_id=event.get("agent_id", self.agent_id),
            actor_id=event.get("actor_id", "runtime"), subject=event.get("subject"),
            agent_instance_id=event.get("agent_instance_id", self.agent_instance_id),
            session_id=event.get("session_id", self.session_id),
            attempt_id=event.get("attempt_id", self.attempt_id),
            segment_id=event.get("segment_id", self.segment_id),
            behavioral_subject=event.get("behavioral_subject"),
            interaction_id=event.get("interaction_id", self.interaction_id),
            parent_session_id=event.get("parent_session_id", self.parent_session_id),
            delegator_agent_id=event.get("delegator_agent_id", self.delegator_agent_id),
            delegate_agent_id=event.get("delegate_agent_id", self.delegate_agent_id),
            delegation_id=event.get("delegation_id", self.delegation_id),
            role=event.get("role", self.role),
            goal_contract_hash=event.get("goal_contract_hash"),
            evidence_refs=event.get("evidence_refs"), invalidates=event.get("invalidates"),
            parent_event=event.get("parent_event"), input_state=event.get("input_state"),
            output_state=event.get("output_state"), uncertainty=event.get("uncertainty"),
            authority_source=event.get("authority_source"), scope=event.get("scope"),
            supersedes=event.get("supersedes"), payload=event.get("payload"))
