#!/usr/bin/env python3
"""Host runtime adapter for CognitiveFrameWorks.

This is the convergence point the review requires: the CLI resolver is a
debugging front end; the actual agent/harness calls the Python API.

    TaskRequest -> resolve() -> RuntimeBundle -> runtime.start(bundle)

`start()` injects the selected compact runtime capsules into the model
context and registers tool/action hooks plus a final-output hook. The
bundles are immutable, per-task, content-addressed snapshots in
$XDG_RUNTIME_DIR/cognitiveframeworks/<task>/<policy-hash>.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONTRACT_ROOT = ROOT / "contracts"
if not LOCAL_CONTRACT_ROOT.exists():
    LOCAL_CONTRACT_ROOT = Path(__file__).resolve().parent / "contracts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util
import importlib.util as _ilu
from runtime_contract.actions import (
    ActionDescriptor, AgentActionRequest, ConfirmationGrant, HostExecutionContext,
    ProtectedResourcePolicy, action_contract_hash, action_digest,
    default_action_registry, effective_action_registry,
)
from runtime_contract.evidence import (CompletionClaim, CompletionPermit,
                                        EvidenceAttestation, EvidenceLedger,
                                        EvidenceRecord, StateSegment,
                                        completion_claim_digest)
from runtime_contract.telemetry import behavior_schema_contract
from runtime_contract.handlers import enforcement_for_guard, handler_for_enforcement
from runtime_contract.stateworks import ExecutionSegment, SegmentStatus
from runtime_contract.validators import evaluate_validator, validator_registry_hash
if "resolve_runtime" in sys.modules:
    resolve_runtime = sys.modules["resolve_runtime"]
else:
    _rr_spec = _ilu.spec_from_file_location("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
    resolve_runtime = _ilu.module_from_spec(_rr_spec)
    sys.modules["resolve_runtime"] = resolve_runtime
    _rr_spec.loader.exec_module(resolve_runtime)

_SISPIS_CALIBRATION = None
_STATEWORK_TRANSITIONS = None
_STATEWORK_TRANSITION_MODULES: Dict[str, Any] = {}

TelemetrySink = Callable[[Dict[str, Any]], None]
SCHEMA_VERSION = "2.2.0"
CATEGORIES = {
    "observation", "assumption", "inference", "decision", "action",
    "tool_result", "validation", "correction", "contradiction", "retry",
    "state_transition", "handoff", "completion", "blocker",
}
PAYLOAD_FIELDS = {"tool_type", "result_class", "evidence_reference", "exit_state", "summary"}
OPTIONAL_EVENT_FIELDS = {
    "parent_event", "subject", "input_state", "output_state", "evidence_refs",
    "uncertainty", "authority_source", "scope", "supersedes", "invalidates", "payload",
    "interaction_id", "parent_session_id", "delegator_agent_id",
    "delegate_agent_id", "delegation_id", "role",
}

_BEHAVIOR_SCHEMA_VALIDATOR = None


class GateDecision(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REQUIRE_REINSPECTION = "require_reinspection"
    REQUIRE_RECOVERY = "require_recovery"


DEFAULT_ACTION_REGISTRY: Dict[str, ActionDescriptor] = default_action_registry()


class LifecycleController:
    """Execute pipeline activation metadata at its declared lifecycle point."""

    def __init__(self, bundle: resolve_runtime.RuntimeBundle) -> None:
        self.bundle = bundle
        self.specs = {item["stage"]: item for item in bundle.kernel_instructions}
        self.active: set[str] = set()
        self.counts: Dict[str, int] = {}

    def activate(self, stage: str, reason: str) -> None:
        spec = self.specs.get(stage)
        if spec is None:
            return
        frequency = spec.get("frequency")
        if frequency in {"per_task", "per_response"} and self.counts.get(stage, 0):
            return
        self.active.add(stage)
        self.counts[stage] = self.counts.get(stage, 0) + 1

    @staticmethod
    def _predicate_matches(predicate: str, context: Mapping[str, Any]) -> bool:
        if predicate == "action.any":
            return True
        if predicate == "action.mutating":
            return context.get("mutability") not in {None, "read"}
        if predicate == "action.external_effect":
            return bool(context.get("network_effect"))
        if predicate == "action.secret_access":
            return bool(context.get("secret_access"))
        if predicate == "action.high_blast_radius":
            return context.get("blast_radius") in {"system", "organization", "global"}
        if predicate == "action.untrusted":
            return bool(context.get("untrusted"))
        if predicate == "task.nontrivial":
            return True
        if predicate.endswith(".any"):
            return True
        return False

    def activate_predicates(self, stage: str, event: str,
                            context: Mapping[str, Any]) -> None:
        spec = self.specs.get(stage, {})
        predicates = spec.get("activation_predicates") or []
        if any(item.get("event") == event and self._predicate_matches(
                str(item.get("predicate")), context) for item in predicates):
            self.activate(stage, f"predicate:{event}")

    def on_task_start(self) -> None:
        for stage, spec in self.specs.items():
            if spec.get("execution_mode") == "preflight":
                self.activate(stage, "task_start")

    def on_model_call(self, *, tool_capable: bool = False,
                      action_hint: Optional[ActionDescriptor] = None) -> None:
        """Boundary before model generation.

        A tool-capable generation must activate FUSE before the model chooses
        an action.  Pure reasoning generations retain the smaller preflight
        surface.
        """
        self.on_task_start()
        if tool_capable:
            self.on_tool_planning(action_hint)

    def on_tool_planning(self, descriptor: Optional[ActionDescriptor] = None) -> None:
        """Expose planning-time FUSE; defer WARD until host classification."""
        self.activate_predicates("fuse", "action", {
            "mutability": "read", "action_planning": True})
        if descriptor is not None:
            self.activate_predicates("ward", "action", descriptor.__dict__)

    def reset_for_segment(self) -> None:
        """Start a fresh model subcall without carrying visible activation.

        A segment is an immutable prompt boundary.  Its lifecycle state must
        not leak from the preceding StateWork, otherwise a supposedly
        exclusive prerequisite chain becomes a single mixed context.
        """
        self.active.clear()
        self.counts.clear()
        self.on_task_start()

    def on_action(self, decision: GateDecision,
                  descriptor: Optional[ActionDescriptor] = None) -> None:
        context = descriptor.__dict__ if descriptor is not None else {}
        self.activate_predicates("fuse", "action", context)
        self.activate_predicates("ward", "action", context)
        if decision != GateDecision.ALLOW:
            self.activate("ward", "action_gate")

    def on_artifact(self, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True) -> None:
        if flow_triggered:
            self.activate("flow", "artifact_trigger")
        if durable_contract_changed:
            self.activate("dox_closeout", "durable_contract")
        if state_changed:
            self.activate("anchor_closeout", "artifact_state")

    def on_completion(self) -> None:
        self.activate("sispis", "completion")


class HostTelemetry:
    """Wires the host runtime into the DP event plane without importing DP
    code: emits schema-shaped events to a per-task NDJSON store (0600). The
    resolver/doctor must never import DigitalPsychology; this adapter only
    writes JSON lines in the DP event schema."""

    def __init__(self, task_id: str, store: Optional[Path] = None,
                 *, expected_schema_hash: Optional[str] = None,
                 expected_schema_version: Optional[str] = None,
                 agent_id: str = "agent", agent_instance_id: Optional[str] = None,
                 session_id: str = "session-unknown", attempt_id: str = "attempt-1",
                 segment_id: str = "segment-0", interaction_id: Optional[str] = None,
                 parent_session_id: Optional[str] = None,
                 delegator_agent_id: Optional[str] = None,
                 delegate_agent_id: Optional[str] = None,
                 delegation_id: Optional[str] = None, role: Optional[str] = None) -> None:
        self.task_id = task_id
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
        schema_path = resolve_runtime.DP_ROOT / "schemas" / "behavior-event.schema.json"
        schema, self.schema_hash, self.schema_version = behavior_schema_contract(schema_path)
        if expected_schema_hash and expected_schema_hash != self.schema_hash:
            raise ValueError(
                "pinned behavior-event schema hash does not match the host artifact")
        if expected_schema_version and expected_schema_version != self.schema_version:
            raise ValueError(
                "pinned behavior-event schema version does not match the host artifact")
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        root = resolve_runtime.runtime_state_dir().resolve()
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
        global _BEHAVIOR_SCHEMA_VALIDATOR
        try:
            import jsonschema
            from jsonschema import Draft202012Validator, FormatChecker
            schema_path = resolve_runtime.DP_ROOT / "schemas" / "behavior-event.schema.json"
            if not schema_path.exists():
                schema_path = LOCAL_CONTRACT_ROOT / "behavior-event.schema.json"
            if _BEHAVIOR_SCHEMA_VALIDATOR is None:
                if not schema_path.exists():
                    raise ValueError("DigitalPsychology behavior-event schema is unavailable")
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                _BEHAVIOR_SCHEMA_VALIDATOR = Draft202012Validator(
                    schema, format_checker=FormatChecker())
            errors = sorted(_BEHAVIOR_SCHEMA_VALIDATOR.iter_errors(event),
                            key=lambda error: list(error.path))
            if errors:
                raise ValueError(f"telemetry violates behavior-event.schema.json: {errors[0].message}")
        except ImportError as exc:
            raise ValueError("jsonschema is required for canonical telemetry validation") from exc

    @classmethod
    def validate_event(cls, event: Dict[str, Any], *, task_id: str,
                       expected_schema_hash: str,
                       expected_schema_version: str) -> None:
        """Validate even custom sinks against the pinned canonical schema."""
        allowed = {"schema_version", "event_id", "category", "event_type",
                   "agent_id", "actor_id", "agent_instance_id", "session_id",
                   "attempt_id", "segment_id", "behavioral_subject", *OPTIONAL_EVENT_FIELDS}
        forbidden = set(event) - allowed
        if forbidden:
            raise ValueError(f"telemetry envelope has forbidden fields: {sorted(forbidden)}")
        schema_path = resolve_runtime.DP_ROOT / "schemas" / "behavior-event.schema.json"
        if not schema_path.exists():
            schema_path = LOCAL_CONTRACT_ROOT / "behavior-event.schema.json"
        _schema, actual_hash, actual_version = behavior_schema_contract(schema_path)
        if actual_hash != expected_schema_hash or actual_version != expected_schema_version:
            raise ValueError("pinned behavior-event schema changed during the task")
        if event.get("schema_version") not in (None, actual_version):
            raise ValueError("telemetry event schema_version disagrees with the pinned schema")
        canonical = {
            "schema_version": actual_version,
            "event_id": event.get("event_id") or f"evt-{uuid.uuid4()}",
            "timestamp": event.get("timestamp") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
        allowed = {"schema_version", "event_id", "category", "event_type", "agent_id",
                   "actor_id", "agent_instance_id", "session_id", "attempt_id",
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
            evidence_refs=event.get("evidence_refs"), invalidates=event.get("invalidates"),
            parent_event=event.get("parent_event"), input_state=event.get("input_state"),
            output_state=event.get("output_state"), uncertainty=event.get("uncertainty"),
            authority_source=event.get("authority_source"), scope=event.get("scope"),
            supersedes=event.get("supersedes"), payload=event.get("payload"))


class _HostCapability:
    """Unforgeable-by-contract capability held by the host adapter."""


class HostIngress:
    """The only public surface that can create host attestations/events.

    Agent-facing runtime methods deliberately reject these operations.  A
    connector owned by the host receives this capability and supplies facts
    from the actual tool/operator channels.
    """

    def __init__(self, session: "RuntimeSession", capability: _HostCapability) -> None:
        self._session = session
        self._capability = capability

    def record_observation(self, observation_type: str,
                           subject_ref: Optional[str] = None,
                           *, details: Optional[Mapping[str, Any]] = None) -> str:
        return self._session._record_observation(
            self._capability, observation_type, subject_ref, details=details)

    def record_tool_result(self, tool_type: str, result_class: str,
                           subject_ref: Optional[str] = None,
                           *, invocation_id: Optional[str] = None,
                           result_digest: Optional[str] = None,
                           actual_result: Optional[Mapping[str, Any]] = None) -> str:
        return self._session._record_tool_result(
            self._capability, tool_type, result_class, subject_ref,
            invocation_id=invocation_id, result_digest=result_digest,
            actual_result=actual_result)

    def record_tool_outcome(self, tool_type: str, result_class: str,
                            subject_ref: Optional[str] = None, **kwargs: Any) -> str:
        """Explicit alias matching the host interceptor contract."""
        return self.record_tool_result(tool_type, result_class, subject_ref, **kwargs)

    def record_validator_result(self, validator_id: str, invocation_id: str,
                                result: Mapping[str, Any],
                                subject_ref: Optional[str] = None,
                                *, claim_digest: Optional[str] = None) -> str:
        return self._session._record_validator_result(
            self._capability, validator_id, invocation_id, result, subject_ref,
            claim_digest=claim_digest)

    def record_operator_observation(self, subject_ref: str, invalidates: str) -> str:
        return self._session._record_operator_observation(
            self._capability, subject_ref, invalidates)

    def record_decision(self, event_type: str, subject_ref: Optional[str],
                        result_class: str) -> str:
        return self._session._record_decision(
            self._capability, event_type, subject_ref, result_class)

    def record_route_decision(self, *, route: str, route_type: str,
                              selection_reason: str = "host_selected") -> str:
        return self._session._record_route_decision(
            self._capability, route=route, route_type=route_type,
            selection_reason=selection_reason)

    def evaluate_route_outcome(self, *, route_decision_id: str, evaluator_id: str,
                               evidence_refs: List[str]) -> str:
        return self._session._evaluate_route_outcome(
            self._capability, route_decision_id=route_decision_id,
            evaluator_id=evaluator_id, evidence_refs=evidence_refs)

    def record_route_outcome(self, *, route_decision_id: str, evaluator_id: str,
                             evidence_refs: List[str],
                             subject_ref: Optional[str] = None) -> str:
        return self._session._record_route_outcome(
            self._capability, route_decision_id=route_decision_id,
            evaluator_id=evaluator_id, evidence_refs=evidence_refs,
            subject_ref=subject_ref)

    def publish_handoff(self, packet: Dict[str, Any], *,
                        observation_context: Optional[Mapping[str, Any]] = None) -> str:
        return self._session._publish_handoff(
            self._capability, packet, observation_context=observation_context)


@dataclass
class RuntimeSession:
    """A started task runtime: injected instructions + hooks."""
    bundle: resolve_runtime.RuntimeBundle
    injected: Dict[str, str] = field(default_factory=dict)
    telemetry: List[TelemetrySink] = field(default_factory=list)
    tool_hooks: List[Callable[[Dict[str, Any]], Optional[GateDecision]]] = field(default_factory=list)
    final_hooks: List[Callable[[Dict[str, Any]], None]] = field(default_factory=list)
    signals: List[Dict[str, Any]] = field(default_factory=list)
    signal_seen: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    statework_states: Dict[tuple[str, str], str] = field(default_factory=dict)
    statework_entry_times: Dict[tuple[str, str], str] = field(default_factory=dict)
    active_statework_ids: set[str] = field(default_factory=set)
    execution_segments: tuple[ExecutionSegment, ...] = ()
    current_segment_index: int = 0
    contradictions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    ledger: EvidenceLedger = field(default_factory=lambda: EvidenceLedger("unbound"))
    _action_registry: Dict[str, ActionDescriptor] = field(default_factory=dict,
                                                          repr=False)
    handoffs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    state_segments: List[StateSegment] = field(default_factory=list)
    _segment_status: Dict[str, SegmentStatus] = field(default_factory=dict,
                                                      repr=False)
    active_lifecycle: set[str] = field(default_factory=set)
    lifecycle_counts: Dict[str, int] = field(default_factory=dict)
    lifecycle: Optional[LifecycleController] = None
    _packet_store: Any = None
    _emit: Optional[TelemetrySink] = None
    host_context: HostExecutionContext = field(default_factory=HostExecutionContext)
    host_invocations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _consumed_grant_ids: set[str] = field(default_factory=set, repr=False)
    session_nonce: str = ""
    session_id: str = ""
    agent_instance_id: str = "agent"
    attempt_id: str = "attempt-1"
    interaction_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    delegator_agent_id: Optional[str] = None
    delegate_agent_id: Optional[str] = None
    delegation_id: Optional[str] = None
    role: Optional[str] = None
    authority_revision: int = 0
    _completion_permit: Optional[CompletionPermit] = field(default=None, repr=False)
    _security_frozen: bool = field(default=False, init=False, repr=False)
    journal_path: Optional[Path] = None
    journal_db_path: Optional[Path] = None
    _host_capability: _HostCapability = field(default_factory=_HostCapability,
                                               init=False, repr=False, compare=False)
    _ledger_token: Any = field(default_factory=object, init=False, repr=False, compare=False)
    route_decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)

    def _host_ingress(self) -> HostIngress:
        """Return the host-only channel to the adapter implementation.

        It is deliberately private on the agent-facing session.  Host
        integrations should use ``CognitiveRuntime``/their adapter handle;
        exposing a public ``session.host`` property made privileged evidence
        ingestion discoverable to an in-process model/plugin caller.
        """
        return HostIngress(self, self._host_capability)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_security_frozen", False) and name in {
                "bundle", "host_context", "_action_registry", "_host_capability",
                "_ledger_token", "_completion_permit", "ledger", "host_invocations",
                "contradictions", "statework_states", "_segment_status",
                "_consumed_grant_ids", "execution_segments", "session_nonce",
                "authority_revision", "session_id", "agent_instance_id", "attempt_id",
                "interaction_id", "parent_session_id", "delegator_agent_id",
                "delegate_agent_id", "delegation_id", "role",
                "journal_path", "journal_db_path"}:
            raise AttributeError(f"runtime security field {name!r} is immutable after start")
        object.__setattr__(self, name, value)

    @property
    def action_registry(self) -> Mapping[str, ActionDescriptor]:
        return MappingProxyType(dict(self._action_registry))

    @property
    def segment_status(self) -> Mapping[str, SegmentStatus]:
        return MappingProxyType(dict(self._segment_status))

    @property
    def completion_permit(self) -> Optional[CompletionPermit]:
        return self._completion_permit

    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Optional[Mapping[str, Any]] = None) -> str:
        if self.lifecycle is not None:
            prior = set(self.active_lifecycle)
            descriptor = self._descriptor(action_hint) if action_hint is not None else None
            self.lifecycle.on_model_call(tool_capable=tool_capable,
                                         action_hint=descriptor)
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
            self._emit_lifecycle_activations(prior)
        return context_for(self)

    def before_tool_planning(self, action_hint: Optional[Mapping[str, Any]] = None) -> str:
        if self.lifecycle is not None:
            prior = set(self.active_lifecycle)
            descriptor = self._descriptor(action_hint) if action_hint is not None else None
            self.lifecycle.on_tool_planning(descriptor)
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
            self._emit_lifecycle_activations(prior)
        return context_for(self)

    def _emit_lifecycle_activations(self, prior: set[str]) -> None:
        for stage in sorted(self.active_lifecycle - prior):
            self._emit_runtime({
                "category": "decision", "event_type": "lifecycle_stage_activated",
                "payload": {"summary": stage, "route": stage,
                            "route_type": "lifecycle_stage",
                            "reason": "registered_predicate"}})

    @property
    def host(self) -> HostIngress:
        raise PermissionError(
            "host ingress is not exposed on the agent session; use the host adapter")

    @property
    def current_segment(self) -> ExecutionSegment:
        return self.execution_segments[self.current_segment_index]

    def advance_segment(self, segment_index: Optional[int] = None) -> ExecutionSegment:
        """Start a new immutable subcall context; prior prompt text is not mutated."""
        target = self.current_segment_index + 1 if segment_index is None else segment_index
        if target < 0 or target >= len(self.execution_segments):
            raise ValueError(f"unknown execution segment {target}")
        if target < self.current_segment_index:
            raise ValueError("execution segments are forward-only")
        current_id = self.current_segment.segment_id
        if self._segment_status.get(current_id) != SegmentStatus.COMPLETED:
            self._segment_status[current_id] = SegmentStatus.BLOCKED
            raise PermissionError(
                f"segment {current_id} cannot advance before its host-attested output is complete")
        next_id = self.execution_segments[target].segment_id
        if target != self.current_segment_index + 1:
            raise PermissionError("execution segments must advance sequentially")
        self.current_segment_index = target
        self._segment_status[next_id] = SegmentStatus.ACTIVE
        self._journal("segment_activated", segment_id=next_id,
                      segment_index=target, statework_id=self.current_segment.statework_id)
        statework_id = self.current_segment.statework_id
        self.active_statework_ids = {statework_id} if statework_id else set()
        if self.lifecycle is not None:
            self.lifecycle.reset_for_segment()
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
        self._emit_runtime({"category": "observation", "event_type": "execution_segment_started",
                   "subject": self.current_segment.subject_ref,
                   "payload": {"summary": self.current_segment.segment_id,
                               "result_class": "active"}})
        return self.current_segment

    def emit(self, event: Dict[str, Any]) -> None:
        """Raw telemetry is not an agent capability."""
        raise PermissionError("raw telemetry emission is host-owned")

    def _emit_runtime(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("agent_id", self.bundle.pinned.get("agent_id", "agent"))
        event.setdefault("agent_instance_id", self.agent_instance_id)
        event.setdefault("session_id", self.session_id)
        event.setdefault("attempt_id", self.attempt_id)
        event.setdefault("segment_id", self.current_segment.segment_id)
        event.setdefault("behavioral_subject", event.get("subject") or
                         self.bundle.policy.subject_ref or f"agent:{self.agent_instance_id}")
        for key in ("interaction_id", "parent_session_id", "delegator_agent_id",
                    "delegate_agent_id", "delegation_id", "role"):
            if getattr(self, key) is not None:
                event.setdefault(key, getattr(self, key))
        if event.get("actor_id") in {"operator", "repository_owner", "service_owner"}:
            raise PermissionError("operator identity is host-owned")
        HostTelemetry.validate_event(
            event, task_id=self.bundle.task_id,
            expected_schema_hash=self.bundle.policy.behavior_event_schema_hash,
            expected_schema_version=self.bundle.policy.behavior_event_schema_version)
        if self._emit:
            self._emit(event)

    def _request(self, action: Mapping[str, Any] | AgentActionRequest) -> AgentActionRequest:
        return action if isinstance(action, AgentActionRequest) else AgentActionRequest.from_mapping(action)

    def _descriptor(self, action: Mapping[str, Any] | AgentActionRequest) -> ActionDescriptor:
        request = self._request(action)
        descriptor = self.action_registry.get(request.binding_id)
        if descriptor is None:
            # Unknown actions are not allowed to self-classify. The host can
            # explicitly register a safe descriptor when it knows the tool.
            return ActionDescriptor(request.binding_id or "unknown", untrusted=True,
                                    required_authority="operator")
        return descriptor.for_request(request)

    def _decision(self, action: Mapping[str, Any] | AgentActionRequest) -> GateDecision:
        request = self._request(action)
        descriptor = self._descriptor(request)
        for adjustment in self.bundle.guard_pack.get("routing_adjustments", ()):
            if (adjustment.get("route_type") == "tool_policy"
                    and adjustment.get("disposition") == "suppress"
                    and adjustment.get("route") == request.binding_id):
                return GateDecision.BLOCK
        for enforcement in self.bundle.guard_pack.get("structural_handlers", {}).values():
            if enforcement.get("mode") not in {"structural", "tool_gate"}:
                continue
            handler = handler_for_enforcement(enforcement)
            result = (handler.gate(self, request, enforcement)
                      if handler and "before_action" in getattr(handler, "hooks", ())
                      else None)
            if result == "block":
                return GateDecision.BLOCK
            if result == "require_reinspection":
                return GateDecision.REQUIRE_REINSPECTION
        confirmation = self.host_context.confirmation_for(request, self._consumed_grant_ids)
        if descriptor.destructive and confirmation is None:
            return GateDecision.REQUIRE_CONFIRMATION
        if descriptor.outside_scope or descriptor.untrusted:
            return GateDecision.BLOCK
        authorities = set(descriptor.allowed_authorities or ())
        if descriptor.required_authority and not authorities:
            authorities = {descriptor.required_authority}
        if authorities and self.host_context.principal not in authorities:
            return GateDecision.REQUIRE_CONFIRMATION
        if descriptor.required_capabilities and not set(descriptor.required_capabilities).issubset(
                set(self.host_context.capabilities)):
            return GateDecision.REQUIRE_CONFIRMATION
        if descriptor.mutability not in {"read", "inspect"}:
            policy = self.host_context.protected_resources
            if tuple(self.host_context.filesystem_scope) != policy.authorized_write_roots:
                policy = ProtectedResourcePolicy(policy.protected_roots,
                                                 tuple(self.host_context.filesystem_scope))
            classification = policy.classify_write(request.arguments)
            if classification != "authorized":
                return GateDecision.BLOCK
        if any(not record["resolved"] and
               (record.get("subject") in {None, request.subject_ref})
               for record in self.contradictions.values()):
            return GateDecision.REQUIRE_REINSPECTION
        return GateDecision.ALLOW

    @staticmethod
    def _more_restrictive(left: GateDecision, right: GateDecision) -> GateDecision:
        rank = {
            GateDecision.ALLOW: 0,
            GateDecision.REQUIRE_CONFIRMATION: 1,
            GateDecision.REQUIRE_REINSPECTION: 2,
            GateDecision.REQUIRE_RECOVERY: 3,
            GateDecision.BLOCK: 4,
        }
        return left if rank[left] >= rank[right] else right

    def before_action(self, action: Dict[str, Any] | AgentActionRequest) -> GateDecision:
        """Structural WARD gate; host executors run only ALLOW decisions."""
        request = self._request(action)
        descriptor = self._descriptor(request)
        decision = self._decision(request)
        self._emit_runtime({"category": "action", "event_type": "action_requested",
                   "subject": request.subject_ref,
                   "payload": {"summary": decision.value,
                               "tool_type": descriptor.tool_type}})
        for hook in self.tool_hooks:
            hook_decision = hook({"tool_binding_id": request.binding_id,
                                  "arguments": dict(request.arguments),
                                  "subject_ref": request.subject_ref})
            if hook_decision is not None:
                if not isinstance(hook_decision, GateDecision):
                    raise ValueError("tool hooks must return GateDecision or None")
                decision = self._more_restrictive(decision, hook_decision)
        if self.lifecycle is not None:
            prior = set(self.active_lifecycle)
            self.lifecycle.on_action(decision, descriptor=descriptor)
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
            self._emit_lifecycle_activations(prior)
        confirmation = self.host_context.confirmation_for(request, self._consumed_grant_ids)
        if decision == GateDecision.ALLOW and confirmation is not None:
            self._consumed_grant_ids.add(confirmation.grant_id)
            self._journal("confirmation_consumed", grant_id=confirmation.grant_id)
        return decision

    def before_retry(self, request: Mapping[str, Any] | AgentActionRequest) -> GateDecision:
        """Dispatch retry-specific structural handlers at the host boundary."""
        action = self._request(request)
        for enforcement in self.bundle.guard_pack.get("structural_handlers", {}).values():
            if enforcement.get("mode") != "structural":
                continue
            handler = handler_for_enforcement(enforcement)
            if handler and "before_retry" in getattr(handler, "hooks", ()):
                result = handler.before_retry(self, action, enforcement)
                if result == "block":
                    return GateDecision.BLOCK
                if result == "require_reinspection":
                    return GateDecision.REQUIRE_REINSPECTION
        return GateDecision.ALLOW

    def on_artifact(self, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True) -> None:
        if self.lifecycle is not None:
            self.lifecycle.on_artifact(
                flow_triggered=flow_triggered,
                durable_contract_changed=durable_contract_changed,
                state_changed=state_changed)
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)

    def _host_event(self, event: Dict[str, Any], *, actor_id: str = "runtime") -> str:
        """Emit an event through a host-owned identity channel."""
        event = dict(event)
        event.setdefault("agent_id", self.bundle.pinned.get("agent_id", "agent"))
        event.setdefault("agent_instance_id", self.agent_instance_id)
        event.setdefault("session_id", self.session_id)
        event.setdefault("attempt_id", self.attempt_id)
        event.setdefault("segment_id", self.current_segment.segment_id)
        event.setdefault("behavioral_subject", event.get("subject") or
                         self.bundle.policy.subject_ref or f"agent:{self.agent_instance_id}")
        for key in ("interaction_id", "parent_session_id", "delegator_agent_id",
                    "delegate_agent_id", "delegation_id", "role"):
            if getattr(self, key) is not None:
                event.setdefault(key, getattr(self, key))
        event["actor_id"] = actor_id
        # ``emit`` intentionally rejects privileged identities.  This private
        # path performs the same validation while allowing a trusted adapter
        # to attribute an operator observation correctly.
        HostTelemetry.validate_event(
            event, task_id=self.bundle.task_id,
            expected_schema_hash=self.bundle.policy.behavior_event_schema_hash,
            expected_schema_version=self.bundle.policy.behavior_event_schema_version)
        if self._emit:
            owner = getattr(self._emit, "__self__", None)
            if isinstance(owner, HostTelemetry):
                owner._emit_host_event(event)
            else:
                self._emit(event)
        return event.get("event_id", "")

    def _journal(self, event_type: str, **fields: Any) -> None:
        """Commit SQLite/WAL authority first, then derive NDJSON.

        SQLite is the sole recovery authority.  NDJSON is an inspectable
        projection and may temporarily lag after a crash without changing
        authorization or resume semantics.
        """
        if self.journal_path is None and self.journal_db_path is None:
            return
        record = {"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "task_id": self.bundle.task_id, "session_nonce": self.session_nonce,
                  "session_id": self.session_id,
                  "agent_instance_id": self.agent_instance_id,
                  "attempt_id": self.attempt_id,
                  "policy_hash": self.bundle.pinned["policy_hash"],
                  "event_type": event_type, **fields}
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        journal_parent = (self.journal_path or self.journal_db_path).parent
        journal_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(journal_parent, 0o700)
        if self.journal_db_path is not None:
            with sqlite3.connect(self.journal_db_path) as database:
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("PRAGMA synchronous=FULL")
                database.execute(
                    "CREATE TABLE IF NOT EXISTS authority_events "
                    "(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_json TEXT NOT NULL)")
                database.execute("INSERT INTO authority_events(event_json) VALUES (?)",
                                 (encoded,))
                database.commit()
            os.chmod(self.journal_db_path, 0o600)
            if self.journal_path is not None:
                with sqlite3.connect(self.journal_db_path) as database:
                    rows = database.execute(
                        "SELECT event_json FROM authority_events ORDER BY seq").fetchall()
                fd, temporary = tempfile.mkstemp(
                    dir=str(self.journal_path.parent),
                    prefix=f".{self.journal_path.name}.", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        for (event_json,) in rows:
                            stream.write(event_json + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, self.journal_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                os.chmod(self.journal_path, 0o600)
        elif self.journal_path is not None:
            # Legacy/test-only sessions without a database retain a simple
            # journal. Normal start() always provisions the SQLite authority.
            with self.journal_path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(self.journal_path, 0o600)

    def _record_attestation(self, *, evidence_type: str, subject_ref: Optional[str],
                            issuer: str, event: Dict[str, Any],
                            invocation_id: Optional[str], content: Any,
                            claim_digest: Optional[str] = None) -> str:
        event = dict(event)
        event_id = event.setdefault("event_id", f"evt-{uuid.uuid4()}")
        self._host_event(event)
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        attestation = EvidenceAttestation(
            # The canonical host event is the evidence reference consumed by
            # evaluators and downstream state machines.
            evidence_id=event_id, task_id=self.bundle.task_id,
            subject_ref=subject_ref, evidence_type=evidence_type, issuer=issuer,
            source_event_id=event_id, invocation_id=invocation_id,
            observed_at=observed_at,
            content_digest=hashlib.sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
                .encode("utf-8")).hexdigest(),
            claim_digest=claim_digest)
        evidence_id = self.ledger.record_attestation(attestation,
                                                     writer_token=self._ledger_token)
        self._journal("evidence_attested", evidence_id=evidence_id,
                      evidence_type=evidence_type, subject_ref=subject_ref,
                      invocation_id=invocation_id,
                      issuer=attestation.issuer,
                      source_event_id=attestation.source_event_id,
                      observed_at=attestation.observed_at,
                      content_digest=attestation.content_digest,
                      claim_digest=attestation.claim_digest,
                      invalidated=attestation.invalidated)
        return evidence_id

    def record_evidence(self, *_: Any, **__: Any) -> str:
        raise PermissionError("agents cannot mint evidence attestations")

    def _require_host(self, capability: _HostCapability) -> None:
        if capability is not self._host_capability:
            raise PermissionError("evidence ingestion requires the host capability")

    def _record_route_decision(self, capability: _HostCapability, *, route: str,
                               route_type: str,
                               selection_reason: str = "host_selected",
                               event_type: str = "route_selected") -> str:
        self._require_host(capability)
        if not route or route_type not in {"stage", "statework", "flow", "specialist",
                                           "guard", "tool_policy", "lifecycle_stage"}:
            raise ValueError("route decision requires a typed route and route_type")
        decision_id = f"route-{uuid.uuid4()}"
        experiment = self.bundle.guard_pack.get("routing_experiment") or {}
        self.route_decisions[decision_id] = {
            "decision_id": decision_id, "route": route, "route_type": route_type,
            "policy_hash": self.bundle.pinned.get("policy_hash"),
            "selection_reason": selection_reason,
            "candidate_profile_hash": experiment.get("candidate_profile_hash"),
            "candidate_adjustment_applied": experiment.get("candidate_adjustment_applied", False),
            "comparison_context_hash": experiment.get("comparison_context_hash"),
        }
        self._host_event({
            "event_id": decision_id, "category": "decision", "event_type": event_type,
            "subject": self.bundle.policy.subject_ref,
            "payload": {"summary": route, "route": route, "route_type": route_type,
                        "route_decision_id": decision_id, "policy_hash": self.bundle.pinned.get("policy_hash"),
                        "reason": selection_reason}})
        return decision_id

    def _evaluate_route_outcome(self, capability: _HostCapability, *,
                                route_decision_id: str, evaluator_id: str,
                                evidence_refs: List[str]) -> str:
        self._require_host(capability)
        decision = self.route_decisions.get(route_decision_id)
        if decision is None:
            raise ValueError("route outcome requires a current host route decision")
        if evaluator_id != "route-success-v1":
            raise ValueError(f"unknown route evaluator {evaluator_id!r}")
        resolved = self.ledger.resolve(list(evidence_refs), subject_ref=self.bundle.policy.subject_ref)
        outcome = "bad"
        for ref in resolved:
            record = self.ledger.get(ref["ref"])
            if record is None or record.kind != "validation_result":
                continue
            invocation = self.host_invocations.get(record.invocation_id or "", {})
            actual = invocation.get("actual_result") or {}
            if str(actual.get("status") or actual.get("result") or "").lower() in {"pass", "passed", "success", "ok"}:
                outcome = "good"
                break
        event_id = f"evt-{uuid.uuid4()}"
        self._host_event({
            "event_id": event_id, "category": "decision", "event_type": "route_outcome",
            "subject": self.bundle.policy.subject_ref,
            "evidence_refs": list(evidence_refs),
            "payload": {"summary": decision["route"], "route": decision["route"],
                        "route_type": decision["route_type"], "route_decision_id": route_decision_id,
                        "evaluator_id": evaluator_id, "outcome_source": f"evaluator:{evaluator_id}",
                        "outcome": outcome, "policy_hash": decision["policy_hash"],
                        "candidate_profile_hash": decision.get("candidate_profile_hash"),
                        "candidate_adjustment_applied": decision.get("candidate_adjustment_applied", False),
                        "comparison_context_hash": decision.get("comparison_context_hash"),
                        "reason": "registered_host_evaluator"}})
        return event_id

    def _record_route_outcome(self, capability: _HostCapability, *,
                              route_decision_id: str, evaluator_id: str,
                              evidence_refs: List[str],
                              subject_ref: Optional[str] = None) -> str:
        """Compatibility spelling for evaluator-owned route outcomes."""
        self._require_host(capability)
        return self._evaluate_route_outcome(
            capability, route_decision_id=route_decision_id,
            evaluator_id=evaluator_id, evidence_refs=evidence_refs)

    def _record_observation(self, capability: _HostCapability, observation_type: str,
                            subject_ref: Optional[str] = None,
                            *, details: Optional[Mapping[str, Any]] = None) -> str:
        """Record a typed host observation; type is selected by this adapter."""
        self._require_host(capability)
        if observation_type in {"boundary_evidence", "completion_boundary", "tool_result"}:
            raise ValueError("reserved evidence types require their dedicated host adapter")
        if observation_type in {"fresh_evidence", "fresh_reinspection", "contradiction"}:
            raise ValueError("fresh contradiction evidence requires a host tool/validator result")
        return self._record_attestation(
            evidence_type=observation_type[:64],
            subject_ref=subject_ref,
            issuer="host:observation",
            event={"category": "observation", "event_type": "host_observation",
                   "subject": subject_ref,
                   "payload": {"summary": observation_type[:512],
                               "result_class": "observed"}},
            invocation_id=None,
            content={"observation_type": observation_type, "details": dict(details or {})})

    def _record_tool_result(self, capability: _HostCapability, tool_type: str,
                            result_class: str, subject_ref: Optional[str] = None,
                            *, invocation_id: Optional[str] = None,
                            result_digest: Optional[str] = None,
                            actual_result: Optional[Mapping[str, Any]] = None) -> str:
        """Record a tool result from the host execution channel."""
        self._require_host(capability)
        invocation_id = invocation_id or f"invocation-{uuid.uuid4()}"
        evidence_id = self._record_attestation(
            evidence_type="tool_result", subject_ref=subject_ref,
            issuer=f"host:tool:{tool_type[:64]}",
            event={"category": "tool_result", "event_type": "tool_completed",
                   "subject": subject_ref,
                   "payload": {"tool_type": tool_type[:64],
                               "result_class": result_class[:64]}},
            invocation_id=invocation_id,
            content={"tool_type": tool_type, "result_class": result_class,
                     "result_digest": result_digest or ""})
        self.host_invocations[invocation_id] = {
            "tool_type": tool_type, "result_class": result_class,
            "subject_ref": subject_ref, "evidence_id": evidence_id,
            "actual_result": dict(actual_result or {}),
        }
        self._journal("tool_invocation_completed", invocation_id=invocation_id,
                      tool_type=tool_type, result_class=result_class,
                      subject_ref=subject_ref, evidence_id=evidence_id,
                      result_digest=result_digest or "",
                      actual_result_digest=hashlib.sha256(
                          json.dumps(dict(actual_result or {}), sort_keys=True,
                                     separators=(",", ":"), default=str).encode("utf-8")
                      ).hexdigest())
        return evidence_id

    def record_tool_result(self, *_: Any, **__: Any) -> str:
        raise PermissionError("tool evidence must come from the host execution adapter")

    def _record_validator_result(self, capability: _HostCapability, validator_id: str,
                                 invocation_id: str, result: Mapping[str, Any],
                                 subject_ref: Optional[str] = None,
                                 *, claim_digest: Optional[str] = None) -> str:
        """Mint completion evidence only for a registered host validator."""
        self._require_host(capability)
        invocation = self.host_invocations.get(invocation_id)
        if invocation is None:
            raise ValueError("validator requires a completed host invocation")
        if not isinstance(result, Mapping):
            raise TypeError("validator result must be a typed mapping, not a caller boolean")
        pinned_validator_hash = self.bundle.pinned.get("contracts", {}).get(
            "validator_registry_hash")
        if pinned_validator_hash != validator_registry_hash():
            raise PermissionError("validator registry changed after policy composition")
        actual_result = invocation.get("actual_result") or {}
        if actual_result and dict(actual_result) != dict(result):
            raise ValueError("validator input does not match the host-captured tool result")
        typed_result = actual_result or dict(result)
        validator_result = evaluate_validator(validator_id, typed_result)
        if not validator_result.passed:
            raise ValueError(f"validator {validator_id!r} rejected the supplied result")
        evidence_type = validator_result.evidence_type
        if evidence_type == "completion_boundary" and not claim_digest:
            raise ValueError("completion validation requires a claim digest")
        if subject_ref is not None and invocation["subject_ref"] not in (None, subject_ref):
            raise ValueError("validator subject does not match the invocation")
        subject_ref = subject_ref or invocation["subject_ref"]
        return self._record_attestation(
            evidence_type=evidence_type, subject_ref=subject_ref,
            issuer=f"validator:{validator_id}",
            event={"category": "validation",
                   "event_type": ("completion_boundary_validated"
                                   if evidence_type == "completion_boundary"
                                   else "validator_result"),
                   "subject": subject_ref,
                   "payload": {"summary": "host validator passed",
                               "result_class": evidence_type}},
            invocation_id=invocation_id,
            content={"validator_id": validator_id, "invocation_id": invocation_id,
                     "tool_evidence_id": invocation["evidence_id"],
                     "result": typed_result},
            claim_digest=claim_digest)

    def record_state_transition(self, statework_id: str, subject_ref: str,
                                output_state: str, *,
                                trigger: Optional[str] = None,
                                evidence_refs: Optional[List[str]] = None,
                                input_state: Optional[str] = None) -> GateDecision:
        """Structurally enforce active StateWork machine transitions."""
        for enforcement in self.bundle.guard_pack.get("structural_handlers", {}).values():
            if enforcement.get("mode") not in {"structural", "state_transition"}:
                continue
            handler = handler_for_enforcement(enforcement)
            if handler and "before_transition" in getattr(handler, "hooks", ()):
                result = handler.gate(self, AgentActionRequest(
                    binding_id=f"statework:{statework_id}",
                    arguments={"output_state": output_state, "trigger": trigger},
                    subject_ref=subject_ref), enforcement)
                if result == "block":
                    return GateDecision.BLOCK
                if result == "require_reinspection":
                    return GateDecision.REQUIRE_REINSPECTION
        statework = next((item for item in self.bundle.stateworks
                          if item.get("id") == statework_id), None)
        if statework is None:
            self._emit_runtime({"category": "state_transition", "event_type": "transition_attempted",
                       "subject": subject_ref, "input_state": input_state,
                       "output_state": output_state,
                       "payload": {"summary": "unknown StateWork", "result_class": "blocked"}})
            return GateDecision.BLOCK
        active_ids = getattr(self, "active_statework_ids", set())
        if active_ids and statework_id not in active_ids:
            self._emit_runtime({"category": "blocker", "event_type": "inactive_statework_transition",
                       "subject": subject_ref,
                       "payload": {"summary": statework_id[:128], "result_class": "blocked"}})
            return GateDecision.BLOCK
        control = _statework_transition_engines(
            Path(self.bundle.statework_root or resolve_runtime.COW_ROOT))
        contract = statework.get("transitions_contract") or {}
        if not contract:
            raise ValueError(f"StateWork {statework_id!r} has no pinned transition contract")
        engine = control.TransitionEngine(contract)
        state_key = (statework_id, subject_ref)
        current = self.statework_states.get(state_key, contract["initial_state"])
        if input_state is not None and input_state != current:
            self._emit_runtime({"category": "blocker", "event_type": "state_conflict",
                       "subject": subject_ref, "input_state": input_state,
                       "output_state": output_state,
                       "payload": {"summary": "tracked state differs from asserted origin",
                                   "result_class": "re-observation required"}})
            return GateDecision.REQUIRE_REINSPECTION
        origin = input_state or current
        entry_time = self.statework_entry_times.get(state_key)
        matching_edges = engine.matching_edges(origin, output_state, trigger)
        freshness = (matching_edges[0].get("evidence_freshness")
                     if len(matching_edges) == 1 else None) or {}
        freshness_mode = freshness.get("mode", "after_state_entry")
        try:
            verified_evidence = self.ledger.resolve(
                list(evidence_refs or []), subject_ref=subject_ref,
                fresh_after=entry_time if freshness_mode == "after_state_entry" else None)
            max_age = freshness.get("max_age_seconds")
            if max_age is not None:
                now = datetime.now(timezone.utc)
                for item in verified_evidence:
                    observed = datetime.fromisoformat(
                        str(item["observed_at"]).replace("Z", "+00:00"))
                    if (now - observed).total_seconds() > int(max_age):
                        raise ValueError("transition evidence exceeds its declared max age")
        except ValueError as exc:
            self._emit_runtime({"category": "blocker", "event_type": "unverified_evidence",
                       "subject": subject_ref,
                       "payload": {"summary": str(exc)[:512], "result_class": "blocked"}})
            return GateDecision.BLOCK
        decision = engine.transition(origin, output_state, trigger=trigger,
                                     evidence_refs=verified_evidence)
        self._emit_runtime({"category": "state_transition", "event_type": "transition_attempted",
                   "subject": subject_ref, "input_state": origin, "output_state": output_state,
                   "payload": {"summary": decision.reason,
                               "result_class": "allowed" if decision.allowed else "blocked"}})
        if not decision.allowed:
            return GateDecision.BLOCK
        self.statework_states[state_key] = output_state
        entry_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.statework_entry_times[state_key] = entry_time
        state_segment = StateSegment(
            segment_id=f"segment-{uuid.uuid4()}", task_id=self.bundle.task_id,
            subject_ref=subject_ref, input_state=origin, output_state=output_state,
            evidence_refs=tuple(item["ref"] for item in verified_evidence),
            recorded_at=entry_time, statework_id=statework_id)
        self.state_segments.append(StateSegment(
            segment_id=state_segment.segment_id, task_id=state_segment.task_id,
            subject_ref=state_segment.subject_ref, input_state=state_segment.input_state,
            output_state=state_segment.output_state,
            evidence_refs=state_segment.evidence_refs,
            recorded_at=state_segment.recorded_at, statework_id=state_segment.statework_id))
        self._journal("state_transition_allowed", statework_id=statework_id,
                      subject_ref=subject_ref, input_state=origin,
                      output_state=output_state, evidence_refs=[item["ref"] for item in verified_evidence],
                      recorded_at=entry_time, segment_id=state_segment.segment_id)
        return GateDecision.ALLOW

    def _publish_handoff(self, capability: _HostCapability, packet: Dict[str, Any], *,
                         observation_context: Optional[Mapping[str, Any]] = None) -> str:
        self._require_host(capability)
        if packet.get("task_id") != self.bundle.task_id:
            raise ValueError("handoff packet belongs to another task")
        expected_subject = self.bundle.policy.subject_ref
        if not expected_subject or packet.get("subject_ref") != expected_subject:
            raise ValueError("handoff packet subject is not the authoritative task subject")
        active_producer = self.current_segment.statework_id
        if packet.get("producer") != active_producer:
            raise ValueError("handoff producer is not the active StateWork segment")
        expected_packet = self.current_segment.output_packet
        if expected_packet and packet.get("packet_type") != expected_packet:
            raise ValueError("handoff packet does not match the active segment output")
        if not packet.get("observed_at"):
            raise ValueError("handoff packet requires an observation timestamp")
        evidence_refs = packet.get("evidence_refs") or []
        if not evidence_refs:
            raise ValueError("host handoffs require observed evidence references")
        self.ledger.resolve(list(evidence_refs), subject_ref=packet.get("subject_ref"))
        transition_contract = next(
            (item.get("transitions_contract") for item in self.bundle.stateworks
             if item.get("id") == active_producer), {}) or {}
        current_state = self.statework_states.get(
            (active_producer, packet.get("subject_ref")))
        if transition_contract and current_state not in set(
                transition_contract.get("completion_states") or ()):
            raise PermissionError("handoff requires the producer StateWork to reach a terminal state")
        if self._packet_store is None:
            control = _statework_transition_engines(
                Path(self.bundle.statework_root or resolve_runtime.COW_ROOT))
            packet_root = resolve_runtime.runtime_state_dir() / "packets"
            safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.bundle.task_id)[:128] or "task"
            self._packet_store = control.PacketStore(
                packet_registry_path=Path(self.bundle.statework_root or resolve_runtime.COW_ROOT) /
                "schemas" / "packet-registry.yaml",
                storage_path=packet_root / f"{safe_task}.json")
        if observation_context is not None and not self._packet_store.validate_current(
                packet, observation_context=observation_context):
            raise ValueError("handoff packet is not current under the host observation")
        packet_id = self._packet_store.put(packet)
        evidence_id = self._record_attestation(
            evidence_type="handoff", subject_ref=packet.get("subject_ref"),
            issuer="host:handoff",
            event={"event_id": f"evt-{uuid.uuid4()}", "category": "handoff",
                   "event_type": "typed_handoff", "subject": packet.get("subject_ref"),
                   "payload": {"summary": packet.get("packet_type", "")[:512]}},
            invocation_id=None, content=packet)
        self.handoffs[packet_id] = dict(packet)
        self._segment_status[self.current_segment.segment_id] = SegmentStatus.COMPLETED
        self._journal("handoff_attested", packet_id=packet_id,
                      segment_id=self.current_segment.segment_id,
                      producer=packet.get("producer"), subject_ref=packet.get("subject_ref"))
        self._emit_runtime({"category": "handoff", "event_type": "handoff_attested",
                   "subject": packet.get("subject_ref"), "evidence_refs": [evidence_id],
                   "payload": {"summary": packet.get("packet_type", "")[:512]}})
        return packet_id

    def publish_handoff(self, *_: Any, **__: Any) -> str:
        raise PermissionError("handoff publication must enter through the host ingress channel")

    def consume_handoff(self, packet_id: str, *, expected_packet_type: str,
                        expected_subject_ref: str,
                        observation_context: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if self._packet_store is None:
            control = _statework_transition_engines(
                Path(self.bundle.statework_root or resolve_runtime.COW_ROOT))
            packet_root = resolve_runtime.runtime_state_dir() / "packets"
            safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.bundle.task_id)[:128] or "task"
            self._packet_store = control.PacketStore(
                packet_registry_path=Path(self.bundle.statework_root or resolve_runtime.COW_ROOT) /
                "schemas" / "packet-registry.yaml",
                storage_path=packet_root / f"{safe_task}.json")
        packet = (self._packet_store.get_current(
            expected_packet_type, task_id=self.bundle.task_id,
            subject_ref=expected_subject_ref,
            observation_context=observation_context)
                  if self._packet_store is not None else None)
        if self.current_segment.input_packet and expected_packet_type != self.current_segment.input_packet:
            raise ValueError("handoff packet does not match the active segment input")
        if packet is None:
            raise ValueError(f"unknown handoff packet {packet_id!r}")
        if packet.get("packet_id") != packet_id:
            raise ValueError("handoff packet id does not match the current packet")
        if packet.get("producer") not in {item.get("id") for item in self.bundle.stateworks}:
            raise ValueError("handoff producer is not part of the task StateWork plan")
        return packet

    def record_handoff(self, subject: str, packet_type: str) -> None:
        raise ValueError("record_handoff is metadata-only; publish_handoff(packet) is required")

    def _record_operator_observation(self, capability: _HostCapability,
                                     subject_ref: str, invalidates: str) -> str:
        self._require_host(capability)
        prior_evidence = self.ledger.get(invalidates)
        object.__setattr__(self, "authority_revision", self.authority_revision + 1)
        if prior_evidence is not None:
            invalidation_timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            # Journal the authority mutation before exposing the contradiction
            # as committed.  Resume replay applies this event to the ledger.
            self._journal("evidence_invalidated", evidence_id=invalidates,
                          invalidated_by="host:operator", subject_ref=subject_ref,
                          timestamp=invalidation_timestamp,
                          authority_revision=self.authority_revision)
            self.ledger.invalidate(invalidates, writer_token=self._ledger_token)
        if (self._completion_permit is not None
                and (prior_evidence is None
                     or invalidates in self._completion_permit.evidence_refs
                     or self._completion_permit.subject_ref == subject_ref)):
            self._journal("completion_permit_revoked",
                          claim_digest=self._completion_permit.claim_digest,
                          authority_revision=self.authority_revision)
            object.__setattr__(self, "_completion_permit", None)
        contradiction_id = f"contradiction-{uuid.uuid4()}"
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.contradictions[contradiction_id] = {
            "id": contradiction_id, "subject": subject_ref, "invalidates": invalidates,
            "resolved": False, "timestamp": timestamp}
        self._journal("contradiction_recorded", contradiction_id=contradiction_id,
                      subject_ref=subject_ref, invalidates=invalidates,
                      timestamp=timestamp,
                      authority_revision=self.authority_revision)
        event_id = self._host_event({"event_id": contradiction_id, "category": "contradiction",
                                     "event_type": "operator_observed", "subject": subject_ref,
                                     "invalidates": invalidates}, actor_id="operator")
        self.ledger.record_attestation(EvidenceAttestation(
            evidence_id=event_id, task_id=self.bundle.task_id,
            subject_ref=subject_ref, evidence_type="contradiction",
            issuer="host:operator", source_event_id=event_id,
            invocation_id=None, observed_at=timestamp,
            content_digest=hashlib.sha256(
                json.dumps({"invalidates": invalidates}, sort_keys=True).encode("utf-8")
            ).hexdigest()), writer_token=self._ledger_token)
        return contradiction_id

    def _record_decision(self, capability: _HostCapability, event_type: str,
                         subject_ref: Optional[str], result_class: str) -> str:
        """Record a typed host-observed decision outcome."""
        self._require_host(capability)
        return self._host_event({"event_id": f"evt-{uuid.uuid4()}",
                                 "category": "decision", "event_type": event_type,
                                 "subject": subject_ref,
                                 "payload": {"result_class": result_class[:64]}})

    # Compatibility names fail explicitly rather than accidentally exposing
    # host authority to an agent/plugin caller holding only RuntimeSession.
    def host_record_observation(self, *_: Any, **__: Any) -> str:
        raise PermissionError("use the host ingress channel for observations")

    def host_record_tool_result(self, *_: Any, **__: Any) -> str:
        raise PermissionError("use the host ingress channel for tool results")

    def host_record_validator_result(self, *_: Any, **__: Any) -> str:
        raise PermissionError("use the host ingress channel for validator results")

    def host_record_operator_observation(self, *_: Any, **__: Any) -> str:
        raise PermissionError("use the host ingress channel for operator observations")

    def host_record_decision(self, *_: Any, **__: Any) -> str:
        raise PermissionError("use the host ingress channel for decisions")

    def record_operator_contradiction(self, *_: Any, **__: Any) -> str:
        raise PermissionError("operator observations must enter through the host channel")

    def resolve_contradiction(self, contradiction_id: str,
                              fresh_evidence_refs: List[str]) -> GateDecision:
        record = self.contradictions.get(contradiction_id)
        if record is None or record["resolved"]:
            return GateDecision.BLOCK
        try:
            self.ledger.resolve(fresh_evidence_refs, subject_ref=record["subject"],
                                fresh_after=record["timestamp"],
                                allowed_kinds={"tool_result", "validation_result",
                                               "completion_boundary"})
        except ValueError as exc:
            self._emit_runtime({"category": "blocker", "event_type": "contradiction_resolution_blocked",
                       "subject": record["subject"],
                       "payload": {"summary": str(exc)[:512], "result_class": "reinspection required"}})
            return GateDecision.REQUIRE_REINSPECTION
        record["resolved"] = True
        self._journal("contradiction_resolved", contradiction_id=contradiction_id,
                      evidence_refs=list(fresh_evidence_refs))
        self._emit_runtime({"category": "correction", "event_type": "contradiction_resolved",
                   "subject": record["subject"], "evidence_refs": list(fresh_evidence_refs),
                   "payload": {"summary": contradiction_id[:512], "result_class": "resolved"}})
        return GateDecision.ALLOW

    def record_completion_claim(self, subject: Optional[str], evidence_refs: List[str],
                                *, boundary_type: str = "completion",
                                boundary_target: str = "task",
                                expected_validator: str = "completion-boundary-v1",
                                claim: Optional[CompletionClaim] = None) -> GateDecision:
        for enforcement in self.bundle.guard_pack.get("structural_handlers", {}).values():
            if enforcement.get("mode") not in {"structural", "state_transition"}:
                continue
            handler = handler_for_enforcement(enforcement)
            if handler and "before_completion" in getattr(handler, "hooks", ()):
                result = handler.before_completion(self, claim, enforcement)
                if result == "require_reinspection":
                    self._emit_runtime({
                        "category": "blocker",
                        "event_type": "completion_blocked_by_claim",
                        "subject": subject,
                        "evidence_refs": evidence_refs,
                        "payload": {"summary": "completion requires an explicit claim binding",
                                    "result_class": "boundary evidence required"}})
                    return GateDecision.REQUIRE_REINSPECTION
        if claim is None:
            self._emit_runtime({"category": "blocker", "event_type": "completion_blocked_by_claim",
                       "subject": subject, "evidence_refs": evidence_refs,
                       "payload": {"summary": "completion requires an explicit claim binding",
                                   "result_class": "boundary evidence required"}})
            return GateDecision.REQUIRE_REINSPECTION
        authoritative_subject = self.bundle.policy.subject_ref
        if (not subject or (authoritative_subject is not None
                            and subject != authoritative_subject)):
            self._emit_runtime({
                "category": "blocker", "event_type": "completion_subject_mismatch",
                "subject": subject, "evidence_refs": evidence_refs,
                "payload": {"summary": "completion requires the exact task subject",
                            "result_class": "blocked"}})
            return GateDecision.BLOCK
        if any(not record["resolved"] and (subject is None or record["subject"] == subject)
               for record in self.contradictions.values()):
            self._emit_runtime({"category": "blocker", "event_type": "completion_blocked_by_contradiction",
                       "subject": subject, "evidence_refs": evidence_refs,
                       "payload": {"summary": "relevant operator contradiction is unresolved",
                                   "result_class": "reinspection required"}})
            return GateDecision.REQUIRE_REINSPECTION
        try:
            resolved = self.ledger.resolve(
                list(evidence_refs), subject_ref=subject,
                allowed_kinds={"boundary_evidence", "completion_boundary"})
            boundary_records = [self.ledger.get(ref) for ref in evidence_refs]
            if any(record is None or not record.claim_digest or not record.invocation_id
                   for record in boundary_records):
                raise ValueError("completion evidence is not bound to a claim")
            claim_digest = claim.claim_digest if claim is not None else None
            if claim is not None:
                if (claim.task_id != self.bundle.task_id or claim.subject_ref != subject
                        or claim.boundary_type != boundary_type
                        or claim.boundary_target != boundary_target
                        or claim.expected_validator != expected_validator):
                    raise ValueError("completion claim identity does not match this task")
                if claim.invocation_id not in {
                        record.invocation_id for record in boundary_records}:
                    raise ValueError("completion claim invocation is not the attested invocation")
            for record in boundary_records:
                expected = completion_claim_digest(
                    self.bundle.task_id, subject, boundary_type, boundary_target,
                    expected_validator, record.invocation_id or "")
                if claim_digest is None:
                    claim_digest = record.claim_digest
                if record.claim_digest != claim_digest or record.claim_digest != expected:
                    raise ValueError("completion evidence is bound to a different claim")
        except ValueError as exc:
            self._emit_runtime({"category": "blocker", "event_type": "completion_blocked_by_evidence",
                       "subject": subject, "evidence_refs": evidence_refs,
                       "payload": {"summary": str(exc)[:512], "result_class": "boundary evidence required"}})
            return GateDecision.REQUIRE_REINSPECTION
        self._emit_runtime({"category": "completion", "event_type": "completion_attempted",
                   "subject": subject, "evidence_refs": evidence_refs,
                   "payload": {"summary": "completion claim validated",
                               "evidence_reference": claim_digest or ""}})
        object.__setattr__(self, "_completion_permit", CompletionPermit(
            task_id=self.bundle.task_id,
            policy_hash=self.bundle.pinned["policy_hash"],
            subject_ref=subject,
            claim_digest=claim_digest or "",
            evidence_refs=tuple(evidence_refs),
            issued_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            session_nonce=self.session_nonce,
            authority_revision=self.authority_revision,
            boundary_type=boundary_type, boundary_target=boundary_target,
            expected_validator=expected_validator))
        self._journal("completion_permit_issued", claim_digest=claim_digest,
                      evidence_refs=list(evidence_refs), subject_ref=subject,
                      issued_at=self._completion_permit.issued_at,
                      authority_revision=self.authority_revision,
                      boundary_type=boundary_type,
                      boundary_target=boundary_target,
                      expected_validator=expected_validator)
        return GateDecision.ALLOW

    def emit_signal(self, envelope: Dict[str, Any]) -> None:
        """Validate, store, and emit a task-local canonical signal envelope."""
        calibration = _sispis_calibration()
        contracts = self.bundle.pinned.get("contracts", {})
        calibration._validate_envelope(
            envelope,
            signal_schema=contracts.get("signal_schema"),
            signal_registry=contracts.get("signal_registry"))
        signal_id = envelope["signal_id"]
        prior = self.signal_seen.get(signal_id)
        if prior and prior != envelope:
            raise ValueError(f"conflicting signal envelope {signal_id!r}")
        self.signal_seen[signal_id] = envelope
        self.signals.append(envelope)
        self._emit_runtime({"category": "decision", "event_type": "signal_emitted",
                   "subject": envelope.get("subject_ref"),
                   "payload": {"summary": envelope["signal_type"][:512],
                               "result_class": envelope["required_action"][:64]}})

    def _render_output(self, output: Optional[Dict[str, Any]] = None,
                       *, complete: bool = False, run_final_hooks: bool = True,
                       **kwargs: Any) -> Dict[str, Any]:
        """Render calibrated output for the host or a non-final preview."""
        output = {} if output is None else output
        if not isinstance(output, dict):
            raise ValueError("final output must be a dictionary")
        contracts = self.bundle.pinned.get("contracts", {})
        decision = _sispis_calibration().calibrate(
            self.signals,
            calibration=contracts.get("sispis_calibration"),
            signal_schema=contracts.get("signal_schema"),
            signal_registry=contracts.get("signal_registry"))
        result = {
            **output,
            **kwargs,
            "sispis": decision,
        }
        if run_final_hooks:
            for hook in self.final_hooks:
                hook(result)
        if complete and self.lifecycle is not None:
            self.lifecycle.on_completion()
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
        return result

    def preview_output(self, output: Optional[Dict[str, Any]] = None,
                       **kwargs: Any) -> Dict[str, Any]:
        """Render a pure preview without hooks or completion lifecycle."""
        return self._render_output(output, complete=False, run_final_hooks=False,
                                   **kwargs)

    def final_output(self, *_: Any, **__: Any) -> Dict[str, Any]:
        raise PermissionError("finalization requires a validated completion permit")

    def finish_task(self, output: Optional[Dict[str, Any]] = None,
                    *, permit: Optional[CompletionPermit] = None,
                    **kwargs: Any) -> Dict[str, Any]:
        """Finalize only with a current completion permit and closed segments."""
        permit = permit or self.completion_permit
        if permit is None:
            raise PermissionError("task finalization requires a validated completion permit")
        if (permit.task_id != self.bundle.task_id
                or permit.policy_hash != self.bundle.pinned["policy_hash"]
                or permit.session_nonce != self.session_nonce
                or permit is not self.completion_permit):
            raise PermissionError("completion permit is not bound to this runtime session")
        if permit.authority_revision != self.authority_revision:
            raise PermissionError("completion permit predates a later authority revision")
        try:
            self.ledger.resolve(list(permit.evidence_refs), subject_ref=permit.subject_ref,
                                allowed_kinds={"boundary_evidence", "completion_boundary"})
            boundary_records = [self.ledger.get(ref) for ref in permit.evidence_refs]
            if any(record is None or record.invalidated or not record.claim_digest
                   or not record.invocation_id for record in boundary_records):
                raise ValueError("completion permit evidence is no longer current")
            for record in boundary_records:
                expected = completion_claim_digest(
                    self.bundle.task_id, permit.subject_ref, permit.boundary_type,
                    permit.boundary_target, permit.expected_validator,
                    record.invocation_id or "")
                if record.claim_digest != permit.claim_digest or record.claim_digest != expected:
                    raise ValueError("completion permit evidence claim binding changed")
        except ValueError as exc:
            raise PermissionError(f"completion permit evidence is invalid: {exc}") from exc
        if any(not record["resolved"] for record in self.contradictions.values()):
            raise PermissionError("cannot finalize with unresolved operator contradictions")
        if len(self.execution_segments) == 1 and self.current_segment.statework_id is None:
            self._segment_status[self.current_segment.segment_id] = SegmentStatus.COMPLETED
        if any(status != SegmentStatus.COMPLETED for status in self._segment_status.values()):
            raise PermissionError("cannot finalize before every execution segment completes")
        for statework in self.bundle.stateworks:
            statework_id = statework.get("id")
            contract = statework.get("transitions_contract") or {}
            completion_states = set(contract.get("completion_states") or ())
            if not completion_states:
                continue
            state = self.statework_states.get((statework_id, permit.subject_ref))
            if state not in completion_states:
                raise PermissionError(
                    f"cannot finalize before StateWork {statework_id!r} reaches a terminal state")
        self._journal("task_finished", subject_ref=permit.subject_ref,
                      claim_digest=permit.claim_digest)
        return self._render_output(output, complete=True, **kwargs)


def start(bundle: resolve_runtime.RuntimeBundle, *,
          telemetry_sink: Optional[TelemetrySink] = None,
          telemetry_disabled: bool = False,
          tool_hooks: Optional[List[Callable[[Dict[str, Any]], Optional[GateDecision]]]] = None,
          final_hooks: Optional[List[Callable[[Dict[str, Any]], None]]] = None,
          action_registry: Optional[Mapping[str, ActionDescriptor]] = None,
          host_context: Optional[HostExecutionContext] = None,
          session_nonce: Optional[str] = None,
          session_id: Optional[str] = None,
          resume: bool = False) -> RuntimeSession:
    """Inject selected runtime bytes, attach default host telemetry, and
    persist the immutable snapshot. Only tests may explicitly disable the
    sink; ordinary callers cannot silently turn it off."""
    supplied_context = host_context or HostExecutionContext()
    pinned_contracts = bundle.pinned.get("contracts", {})
    pinned_runtime_hash = pinned_contracts.get("runtime_behavior_hash")
    if pinned_runtime_hash != resolve_runtime.runtime_behavior_hash():
        raise resolve_runtime.SnapshotIntegrityError(
            "runtime enforcement code changed after policy composition")
    if (session_nonce is not None and supplied_context.session_nonce
            and session_nonce != supplied_context.session_nonce):
        raise ValueError("session nonce supplied by host context and resume request differ")
    session_nonce = supplied_context.session_nonce or session_nonce or f"nonce-{uuid.uuid4()}"
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", bundle.task_id)[:128] or "task"
    journal_root = resolve_runtime.runtime_state_dir() / "sessions"
    if resume and session_id is None:
        # Analytics identity is restored from the authority journal; it is
        # never regenerated from the security nonce or inferred from task ID.
        journal_db = journal_root / f"{safe_task}-{session_nonce}.sqlite3"
        if journal_db.exists():
            with sqlite3.connect(journal_db) as database:
                row = database.execute(
                    "SELECT event_json FROM authority_events ORDER BY seq LIMIT 1").fetchone()
            if row:
                session_id = json.loads(row[0]).get("session_id")
    session_id = session_id or f"analytics-{uuid.uuid4()}"
    if supplied_context.confirmation_grants and (
            supplied_context.task_id != bundle.task_id
            or supplied_context.policy_hash != bundle.pinned["policy_hash"]
            or supplied_context.session_nonce != session_nonce):
        raise ValueError("confirmation grants must be bound to this task policy and session")
    bound_context = replace(supplied_context, task_id=bundle.task_id,
                            policy_hash=bundle.pinned["policy_hash"],
                            session_nonce=session_nonce)
    resolved_action_registry = effective_action_registry(
        bundle.action_registry or DEFAULT_ACTION_REGISTRY)
    current_manifest = resolve_runtime.runtime_build_manifest(
        Path(bundle.statework_root or resolve_runtime.COW_ROOT),
        resolved_action_registry)
    pinned_manifest = pinned_contracts.get("runtime_build_manifest") or {}
    if (pinned_contracts.get("runtime_build_manifest_hash")
            != current_manifest.get("manifest_hash")
            or pinned_manifest != current_manifest):
        raise resolve_runtime.SnapshotIntegrityError(
            "runtime build manifest changed after policy composition")
    if action_registry is not None:
        supplied_registry = effective_action_registry(action_registry)
        if action_contract_hash(supplied_registry) != action_contract_hash(
                resolved_action_registry):
            raise ValueError(
                "start-time action registry differs from the registry pinned at resolve()")
    if action_contract_hash(resolved_action_registry) != bundle.policy.action_contract_hash:
        raise resolve_runtime.SnapshotIntegrityError(
            "host action registry changed after policy composition")
    session = RuntimeSession(bundle=bundle, injected={
        key: value[0] if isinstance(value, tuple) else value
        for key, value in bundle.injected_capsules
    }, ledger=EvidenceLedger(bundle.task_id),
       _action_registry=dict(resolved_action_registry),
       host_context=bound_context,
       execution_segments=tuple(
           ExecutionSegment(
               segment_id=str(item.get("segment_id")),
               index=int(item.get("index", index)),
               statework_id=item.get("statework_id"),
               subject_ref=item.get("subject_ref"),
               input_packet=item.get("input_packet"),
               output_packet=item.get("output_packet"),
               exclusive_group=item.get("exclusive_group"),
               lifecycle_stages=tuple(item.get("lifecycle_stages", ())),
           ) for index, item in enumerate(bundle.execution_plan)
       ) or (ExecutionSegment("segment-0", 0, None, bundle.policy.subject_ref),),
       session_nonce=session_nonce,
       session_id=session_id,
       agent_instance_id=bundle.pinned.get("agent_instance_id", bundle.pinned.get("agent_id", "agent")),
       attempt_id=bundle.pinned.get("attempt_id", "attempt-1"),
       interaction_id=bundle.pinned.get("interaction_id"),
       parent_session_id=bundle.pinned.get("parent_session_id"),
       delegator_agent_id=bundle.pinned.get("delegator_agent_id"),
       delegate_agent_id=bundle.pinned.get("delegate_agent_id"),
       delegation_id=bundle.pinned.get("delegation_id"),
       role=bundle.pinned.get("role"))
    session._ledger_token = object()
    session.ledger = EvidenceLedger(bundle.task_id, writer_token=session._ledger_token)
    session._segment_status = {
        item.segment_id: (SegmentStatus.ACTIVE if index == 0 else SegmentStatus.PENDING)
        for index, item in enumerate(session.execution_segments)
    }
    journal_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(journal_root, 0o700)
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", bundle.task_id)[:128] or "task"
    session.journal_path = journal_root / f"{safe_task}-{session_nonce}.ndjson"
    session.journal_db_path = journal_root / f"{safe_task}-{session_nonce}.sqlite3"
    if not resume:
        if session.journal_path.exists() or session.journal_db_path.exists():
            raise FileExistsError(
                f"session journal already exists; use resume_task for {session_nonce!r}")
        session._journal("session_started", segment_id=session.current_segment.segment_id,
                         segment_index=session.current_segment_index,
                         authority_revision=session.authority_revision,
                         segment_status={key: value.value for key, value in session.segment_status.items()})
    session.active_statework_ids = ({session.current_segment.statework_id}
                                    if session.current_segment.statework_id else set())
    session.lifecycle = LifecycleController(bundle)
    session.lifecycle.on_task_start()
    session.active_lifecycle = set(session.lifecycle.active)
    session.lifecycle_counts = dict(session.lifecycle.counts)
    if telemetry_sink is not None:
        session._emit = telemetry_sink
    elif not telemetry_disabled:
        digest = hashlib.sha256(
            f"{bundle.task_id}:{session.session_id}".encode("utf-8")).hexdigest()[:24]
        store = (resolve_runtime.runtime_state_dir() / "telemetry" /
                 f"{digest}.ndjson")
        session._emit = HostTelemetry(
            bundle.task_id, store,
            expected_schema_hash=bundle.policy.behavior_event_schema_hash,
            expected_schema_version=bundle.policy.behavior_event_schema_version,
            agent_id=bundle.pinned.get("agent_id", "agent"),
            agent_instance_id=session.agent_instance_id,
            session_id=session.session_id, attempt_id=session.attempt_id,
            segment_id=session.current_segment.segment_id,
            interaction_id=session.interaction_id,
            parent_session_id=session.parent_session_id,
            delegator_agent_id=session.delegator_agent_id,
            delegate_agent_id=session.delegate_agent_id,
            delegation_id=session.delegation_id, role=session.role).emit
    session.tool_hooks.extend(tool_hooks or [])
    session.final_hooks.extend(final_hooks or [])
    if session._emit is not None:
        session._emit_runtime({
            "category": "decision", "event_type": "policy_composed",
            "subject": bundle.policy.subject_ref,
            "payload": {"summary": "host resolver froze task routing",
                        "result_class": "validated",
                        "policy_hash": bundle.pinned.get("policy_hash"),
                        "routing_profile_hash": bundle.policy.routing_profile_hash}})
        for stage in bundle.kernel:
            session._record_route_decision(
                session._host_capability, route=stage, route_type="stage",
                selection_reason="static_task_match")
        for item in bundle.stateworks:
            statework_id = item.get("id")
            session._record_route_decision(
                session._host_capability, route=str(statework_id), route_type="statework",
                selection_reason="state_requirement", event_type="statework_selected")
            flow = item.get("selected_flow")
            if flow:
                session._record_route_decision(
                    session._host_capability, route=Path(str(flow[0])).parent.name,
                    route_type="flow", selection_reason="static_task_match",
                    event_type="flow_selected")
            for specialist in item.get("selected_specialists", []):
                specialist_path = specialist[0] if isinstance(specialist, (list, tuple)) else specialist.get("path")
                session._record_route_decision(
                    session._host_capability, route=str(specialist_path),
                    route_type="specialist", selection_reason="static_task_match",
                    event_type="specialist_selected")
        for key in bundle.guard_pack.get("assigned_guard_keys", []):
            session._record_route_decision(
                session._host_capability, route=key, route_type="guard",
                selection_reason="static_task_match", event_type="guard_selected")
        for key in bundle.guard_pack.get("suppressed_guard_keys", []):
            session._record_route_decision(
                session._host_capability, route=key, route_type="guard",
                selection_reason="behavioral_profile_suppression", event_type="route_suppressed")
        for adjustment in bundle.guard_pack.get("routing_adjustments", []):
            route = adjustment.get("route")
            route_type = adjustment.get("route_type")
            if not route or not route_type:
                continue
            if adjustment.get("disposition") == "suppress":
                session._record_route_decision(
                    session._host_capability, route=str(route), route_type=str(route_type),
                    selection_reason="behavioral_profile_suppression",
                    event_type="route_suppressed")
            elif adjustment.get("disposition") in {"prefer", "activate"}:
                session._record_route_decision(
                    session._host_capability, route=str(route), route_type=str(route_type),
                    selection_reason="behavioral_profile_preference",
                    event_type="route_preferred")
    if resume:
        _restore_session(session)
    resolve_runtime.write_snapshot(bundle)
    resolve_runtime.write_task_manifest(
        bundle, session_id=session.session_id, attempt_id=session.attempt_id,
        interaction_id=session.interaction_id, parent_session_id=session.parent_session_id,
        delegator_agent_id=session.delegator_agent_id,
        delegate_agent_id=session.delegate_agent_id, delegation_id=session.delegation_id,
        role=session.role)
    object.__setattr__(session, "_security_frozen", True)
    return session


def _journal_records(session: RuntimeSession) -> List[Dict[str, Any]]:
    """Read the durable authority log, preferring the SQLite WAL copy."""
    records: List[Dict[str, Any]] = []
    if session.journal_db_path is not None and session.journal_db_path.exists():
        with sqlite3.connect(session.journal_db_path) as database:
            rows = database.execute(
                "SELECT event_json FROM authority_events ORDER BY seq").fetchall()
        for (encoded,) in rows:
            records.append(json.loads(encoded))
    elif session.journal_path is not None and session.journal_path.exists():
        records = [json.loads(line) for line in
                   session.journal_path.read_text(encoding="utf-8").splitlines() if line]
    if not records:
        raise FileNotFoundError("no durable authority journal exists for this session")
    return records


def _restore_session(session: RuntimeSession) -> None:
    """Replay only host-authenticated authority facts into a new session."""
    records = _journal_records(session)
    for record in records:
        if (record.get("task_id") != session.bundle.task_id
                or record.get("session_nonce") != session.session_nonce
                or record.get("session_id") != session.session_id
                or record.get("agent_instance_id") != session.agent_instance_id
                or record.get("policy_hash") != session.bundle.pinned["policy_hash"]):
            raise PermissionError("session journal is not bound to this task/policy/session")
        event_type = record.get("event_type")
        if event_type == "session_started":
            session.current_segment_index = int(record.get("segment_index", 0))
            session.authority_revision = int(record.get("authority_revision", 0) or 0)
            statuses = record.get("segment_status") or {}
            for segment_id, status in statuses.items():
                session._segment_status[segment_id] = SegmentStatus(status)
        elif event_type == "segment_activated":
            session.current_segment_index = int(record["segment_index"])
            session._segment_status[record["segment_id"]] = SegmentStatus.ACTIVE
        elif event_type == "evidence_attested":
            evidence_id = str(record["evidence_id"])
            session.ledger._records[evidence_id] = EvidenceRecord(
                evidence_id=evidence_id, task_id=session.bundle.task_id,
                kind=str(record["evidence_type"]),
                subject_ref=record.get("subject_ref"),
                observed_at=str(record.get("observed_at") or record["timestamp"]),
                invalidated=bool(record.get("invalidated", False)),
                event_id=record.get("source_event_id"),
                issuer=record.get("issuer"),
                source_event_id=record.get("source_event_id"),
                invocation_id=record.get("invocation_id"),
                content_digest=record.get("content_digest"),
                claim_digest=record.get("claim_digest"))
        elif event_type == "evidence_invalidated":
            evidence_id = str(record["evidence_id"])
            session.ledger.invalidate(evidence_id, writer_token=session._ledger_token)
            session.authority_revision = max(
                session.authority_revision,
                int(record.get("authority_revision", session.authority_revision) or 0))
        elif event_type == "tool_invocation_completed":
            session.host_invocations[str(record["invocation_id"])] = {
                "tool_type": record.get("tool_type", ""),
                "result_class": record.get("result_class", ""),
                "subject_ref": record.get("subject_ref"),
                "evidence_id": record.get("evidence_id"),
                # Raw tool results are deliberately not persisted.  A resumed
                # completion path must receive a fresh host validator input.
                "actual_result": {},
            }
        elif event_type == "confirmation_consumed":
            session._consumed_grant_ids.add(str(record["grant_id"]))
        elif event_type == "contradiction_recorded":
            contradiction_id = str(record["contradiction_id"])
            session.contradictions[contradiction_id] = {
                "id": contradiction_id, "subject": record.get("subject_ref"),
                "invalidates": record.get("invalidates"), "resolved": False,
                "timestamp": record.get("timestamp"),
            }
            session.authority_revision = max(
                session.authority_revision,
                int(record.get("authority_revision", session.authority_revision) or 0))
        elif event_type == "completion_permit_revoked":
            object.__setattr__(session, "_completion_permit", None)
        elif event_type == "contradiction_resolved":
            contradiction = session.contradictions.get(str(record["contradiction_id"]))
            if contradiction is not None:
                contradiction["resolved"] = True
        elif event_type == "state_transition_allowed":
            key = (str(record["statework_id"]), str(record["subject_ref"]))
            session.statework_states[key] = str(record["output_state"])
            session.statework_entry_times[key] = str(record.get("recorded_at") or record["timestamp"])
            session.state_segments.append(StateSegment(
                segment_id=str(record.get("segment_id") or f"restored-{len(session.state_segments)}"),
                task_id=session.bundle.task_id, subject_ref=key[1],
                input_state=str(record["input_state"]), output_state=str(record["output_state"]),
                evidence_refs=tuple(record.get("evidence_refs") or ()),
                recorded_at=session.statework_entry_times[key], statework_id=key[0]))
        elif event_type == "handoff_attested":
            session.handoffs[str(record["packet_id"])] = {
                "packet_id": record["packet_id"], "producer": record.get("producer"),
                "subject_ref": record.get("subject_ref"),
            }
            segment_id = record.get("segment_id")
            if segment_id in session._segment_status:
                session._segment_status[segment_id] = SegmentStatus.COMPLETED
        elif event_type == "completion_permit_issued":
            object.__setattr__(session, "_completion_permit", CompletionPermit(
                task_id=session.bundle.task_id,
                policy_hash=session.bundle.pinned["policy_hash"],
                subject_ref=record.get("subject_ref"),
                claim_digest=str(record.get("claim_digest") or ""),
                evidence_refs=tuple(record.get("evidence_refs") or ()),
                issued_at=str(record.get("issued_at") or record["timestamp"]),
                session_nonce=session.session_nonce,
                authority_revision=int(record.get("authority_revision", 0) or 0),
                boundary_type=str(record.get("boundary_type") or "completion"),
                boundary_target=str(record.get("boundary_target") or "task"),
                expected_validator=str(record.get("expected_validator") or
                                      "completion-boundary-v1")))
    current = session.current_segment
    session.active_statework_ids = ({current.statework_id}
                                    if current.statework_id else set())
    if session.journal_path is not None:
        os.chmod(session.journal_path, 0o600)
    if session.journal_db_path is not None and session.journal_db_path.exists():
        os.chmod(session.journal_db_path, 0o600)


def resume_task(bundle: resolve_runtime.RuntimeBundle, session_nonce: str, **kwargs: Any) -> RuntimeSession:
    """Resume a task only from its durable, policy-bound authority journal."""
    if not session_nonce:
        raise ValueError("resume requires the original session nonce")
    return start(bundle, session_nonce=session_nonce, resume=True, **kwargs)


def _statework_transition_engines(cow_root: Optional[Path] = None):
    """Load validated machine transition contracts for active StateWorks."""
    global _STATEWORK_TRANSITIONS
    root = Path(cow_root or resolve_runtime.COW_ROOT).resolve()
    cache_key = str(root)
    if cache_key in _STATEWORK_TRANSITION_MODULES:
        return _STATEWORK_TRANSITION_MODULES[cache_key]
    module_path = root / "scripts" / "control_plane.py"
    spec = importlib.util.spec_from_file_location("cfw_cow_control_plane_runtime", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cfw_cow_control_plane_runtime"] = module
    spec.loader.exec_module(module)
    _STATEWORK_TRANSITIONS = module
    _STATEWORK_TRANSITION_MODULES[cache_key] = module
    return module


def _sispis_calibration():
    global _SISPIS_CALIBRATION
    if _SISPIS_CALIBRATION is None:
        module_path = ROOT / "SISPIS" / "runtime" / "calibrate.py"
        spec = importlib.util.spec_from_file_location("cfw_sispis_calibration", module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cfw_sispis_calibration"] = module
        spec.loader.exec_module(module)
        _SISPIS_CALIBRATION = module
    return _SISPIS_CALIBRATION


def context_for(session: RuntimeSession) -> str:
    """The model-visible instruction context: kernel order + injected
    capsules + guard rules. This is what measuring 'the final prompt
    actually injected' means — not the bundle JSON file."""
    parts: List[str] = []
    active_stages = [stage for stage in session.bundle.kernel
                     if stage in session.active_lifecycle]
    if not active_stages:
        active_stages = list(session.bundle.kernel[:1])
    parts.append("ACTIVE PIPELINE: " + " -> ".join(active_stages))
    seen_capsules: set[tuple[str, str]] = set()
    for stage_id in active_stages:
        capsule = session.injected.get(stage_id)
        if capsule:
            owner = stage_id.split("_")[0]
            identity = (owner, capsule)
            if identity in seen_capsules:
                continue
            seen_capsules.add(identity)
            parts.append(f"\n===== {stage_id.upper()} =====\n{capsule}")
    active_ids = session.active_statework_ids
    for key, capsule in session.injected.items():
        if not key.startswith("statework"):
            continue
        if active_ids and not any(
                key.startswith(f"statework:{sid}")
                or key.startswith(f"statework-flow:{sid}:")
                or key.startswith(f"statework-specialist:{sid}:")
                for sid in active_ids):
            continue
        parts.append(f"\n===== {key.upper()} =====\n{capsule}")
    guards = session.bundle.guard_pack.get("guards", [])
    prompt_guards = [g for g in guards
                     if enforcement_for_guard(g).get("mode") == "prompt"]
    if prompt_guards:
        parts.append("\n===== GUARD RULES =====\n" +
                     "\n".join(f"- {g['rule']}" for g in prompt_guards))
    handoff = session.bundle.handoff_plan
    if handoff:
        # Only the current segment's handoff is model-visible.  A complete
        # prerequisite chain would reveal future StateWorks in the same
        # prompt, defeating exclusive segment activation.
        active_handoff = handoff[min(session.current_segment_index, len(handoff) - 1)]
        parts.append("\n===== ACTIVE HANDOFF =====\n" +
                     f"- {active_handoff['producer']} -> {active_handoff['packet']}")
    return "\n".join(parts)


if __name__ == "__main__":
    task_id = sys.argv[1] if len(sys.argv) > 1 else "task-default"
    shape = sys.argv[2] if len(sys.argv) > 2 else "implement"
    raw_domains = sys.argv[3] if len(sys.argv) > 3 else ""
    domains = [d.strip() for d in raw_domains.split(",") if d.strip()]
    bundle = resolve_runtime.resolve(task_id, shape, domains)
    session = start(bundle)
    print(f"task:     {task_id}")
    print(f"profile:  {shape}")
    print(f"kernel:   {' -> '.join(bundle.kernel)}")
    print(f"capsules: {', '.join(bundle.injected_capsules) or '(none)'}")
    ctx = context_for(session)
    print(f"context:  {len(ctx.split())} words injected into model")
    print(f"snapshot: {resolve_runtime.write_snapshot(bundle)}")
    print(f"policy:   {bundle.pinned['policy_hash']}")
