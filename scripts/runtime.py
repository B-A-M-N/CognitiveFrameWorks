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
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import uuid
import yaml
import yaml
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
    ActionDescriptor, ActionStrategy, AgentActionRequest, ConfirmationGrant, HostExecutionContext,
    ProtectedResourcePolicy, action_contract_hash, action_digest,
    default_action_registry, effective_action_registry,
)
from runtime_contract.evidence import (CompletionClaim, CompletionPermit,
                                        EvidenceAttestation, EvidenceLedger,
                                        EvidenceRecord, StateSegment, TerminationPermit,
                                        completion_claim_digest)
from runtime_contract.convergence import (AttemptDescriptor, ConvergenceController,
                                           ConvergenceIdentity, SharedBranchLedger,
                                           TERMINAL_DISPOSITIONS)
from runtime_contract.telemetry import DurableTelemetryExporter, behavior_schema_contract
from runtime_contract.handlers import enforcement_for_guard, handler_for_enforcement
from runtime_contract.operator_events import OperatorEvent
from runtime_contract.stateworks import ExecutionSegment, SegmentStatus
from runtime_contract.behavioral_fusion import (
    SemanticFusion, fuse_action_relation, fuse_progress_relation,
    fuse_retry_relation, render_metacognitive_directive,
)
from runtime_contract.metacognition import (
    CriterionEvidenceBinding, MetaDecision, MetacognitiveController,
    MetacognitiveState, SemanticSignals,
)
from runtime_contract.system_one import (ActivityFact, AttemptSemanticDescriptor,
                                         BehavioralJudge, SystemOneCapability,
                                         SystemOneFrame, SystemOneIdentity,
                                         SystemOneJudgment, SystemOnePolicy,
                                         QUESTION_REGISTRY)
from runtime_contract.system_one_state import SystemOneStateProjector
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
SCHEMA_VERSION = "2.5.0"
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
    "application_version", "provider_id", "model_id", "model_revision",
    "model_capability_hash", "harness_id", "harness_version",
    "goal_contract_hash",
}

_BEHAVIOR_SCHEMA_VALIDATOR = None

# Semantic transition authority.  These names are reserved from the generic
# observation API and are only minted by typed host adapters.
AUTHORITATIVE_EVIDENCE_KINDS = {
    "repository_truth", "authority", "repository_evidence", "rollback", "rollback_plan",
}


RUNTIME_MODES = {"standalone_advisory", "standalone_enforcing", "combined_enforcing"}


def _canonical_system_one_status(reason: str, *, has_judgment: bool,
                                 mode: str) -> str:
    if reason in {"ok", "unavailable", "rejected", "stale", "shadow", "disabled"}:
        return reason
    if reason == "disabled":
        return "disabled"
    if reason in {"stale_request", "stale_response"}:
        return "stale"
    if reason in {"ineligible", "calibration_mismatch", "provider_contract_error"}:
        return "rejected"
    if reason in {"uncalibrated_shadow", "uncalibrated", "shadow_mode"} \
            or (has_judgment and mode == "shadow"):
        return "shadow"
    return "ok" if has_judgment else "unavailable"


@dataclass(frozen=True)
class ExecutionIdentity:
    """Immutable identity for one agent execution; never a workflow key."""
    namespace_id: str
    application_instance_id: str
    agent_instance_id: str
    session_id: str
    task_id: str
    attempt_id: str
    interaction_id: Optional[str]

    def __post_init__(self) -> None:
        for name in ("namespace_id", "application_instance_id", "agent_instance_id",
                     "session_id", "task_id", "attempt_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError(f"execution identity field {name} must be a bounded identifier")
        if self.interaction_id is not None and (
                not isinstance(self.interaction_id, str) or not self.interaction_id
                or len(self.interaction_id) > 256):
            raise ValueError("execution identity interaction_id must be null or a bounded identifier")

    def storage_key(self) -> str:
        return "|".join((self.namespace_id, self.application_instance_id,
                         self.agent_instance_id, self.session_id, self.task_id,
                         self.attempt_id, self.interaction_id or ""))

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "namespace_id", "application_instance_id", "agent_instance_id",
            "session_id", "task_id", "attempt_id", "interaction_id")}


@dataclass(frozen=True)
class SharedWorkflowMembership:
    """Explicit authorization to use a shared CSW workflow subject."""
    namespace_id: str
    application_id: str
    application_instance_id: str
    workflow_id: str
    statework_id: str
    subject_ref: str
    authorization_revision: int

    def state_identity(self) -> dict[str, Any]:
        if self.authorization_revision < 1:
            raise ValueError("shared workflow membership requires a positive authorization revision")
        return {
            "namespace_id": self.namespace_id, "application_id": self.application_id,
            "application_instance_id": self.application_instance_id,
            "workflow_id": self.workflow_id, "statework_id": self.statework_id,
            "subject_ref": self.subject_ref,
            "authorization_revision": self.authorization_revision,
        }


class GateDecision(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REQUIRE_REINSPECTION = "require_reinspection"
    REQUIRE_RECOVERY = "require_recovery"
    REQUIRE_CHANGED_HYPOTHESIS = "require_changed_hypothesis"
    REQUIRE_GOAL_REANCHOR = "require_goal_reanchor"


@dataclass(frozen=True)
class ActionPreparation:
    """One host authorization result and its unique request correlation ID."""

    action_request_id: str
    decision: GateDecision
    descriptor: ActionDescriptor


@dataclass(frozen=True)
class RouteReplanRecommendation:
    """A source-attributed recommendation among host-resolved child bundles."""

    route: str
    decision_id: str
    source: str
    source_state_digest: str
    request_digest: str
    sequence: int
    confidence: float
    state_revision: int
    target_statework_id: str
    legal_children: Mapping[str, resolve_runtime.RuntimeBundle] = field(repr=False)
    semantic_decision_ref: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "legal_children",
                           MappingProxyType(dict(self.legal_children)))


@dataclass(frozen=True)
class PolicyEpochRecord:
    epoch_index: int
    parent_policy_hash: str
    child_policy_hash: str
    reason: str
    route_type: str
    selected_route: str
    decision_source: str
    decision_ref: Optional[str]
    semantic_decision_ref: Optional[str]
    state_revision: int

    @property
    def system_one_decision_ref(self) -> str:
        """Compatibility accessor for callers reading older route records."""
        return self.semantic_decision_ref or ""

    def to_dict(self) -> Dict[str, Any]:
        return {"epoch_index": self.epoch_index,
                "parent_policy_hash": self.parent_policy_hash,
                "child_policy_hash": self.child_policy_hash,
                "reason": self.reason, "route_type": self.route_type,
                "selected_route": self.selected_route,
                "decision_source": self.decision_source,
                "decision_ref": self.decision_ref,
                "semantic_decision_ref": self.semantic_decision_ref,
                "state_revision": self.state_revision}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PolicyEpochRecord":
        data = dict(value)
        if "system_one_decision_ref" in data:
            legacy_ref = data.pop("system_one_decision_ref")
            data.setdefault("decision_source", "semantic_advisory")
            data.setdefault("decision_ref", legacy_ref)
            data.setdefault("semantic_decision_ref", legacy_ref)
        return cls(**data)


@dataclass(frozen=True)
class ChildPolicyEpoch:
    record: PolicyEpochRecord
    bundle: resolve_runtime.RuntimeBundle = field(repr=False)


DEFAULT_ACTION_REGISTRY: Dict[str, ActionDescriptor] = default_action_registry()


class LifecycleController:
    """Execute pipeline activation metadata at its declared lifecycle point."""

    def __init__(self, bundle: resolve_runtime.RuntimeBundle) -> None:
        self.bundle = bundle
        self.specs = {item["stage"]: item for item in bundle.kernel_instructions}
        # Each declared frequency has a matching storage lifetime. In
        # particular, temporary wrappers never enter task_active.
        self.task_active: set[str] = set()
        self.segment_active: set[str] = set()
        self.action_active: set[str] = set()
        self.artifact_active: set[str] = set()
        self.response_active: set[str] = set()
        self.counts: Dict[str, int] = {}

    @property
    def active(self) -> set[str]:
        """Everything currently visible to the model in this segment."""
        return (self.task_active | self.segment_active | self.action_active
                | self.artifact_active | self.response_active)

    def activate(self, stage: str, reason: str) -> None:
        spec = self.specs.get(stage)
        if spec is None:
            return
        frequency = spec.get("frequency")
        if frequency == "per_task" and self.counts.get(stage, 0):
            return
        target = {
            "per_task": self.task_active,
            "per_action": self.action_active,
            "per_artifact": self.artifact_active,
            "per_edit": self.artifact_active,
            "per_response": self.response_active,
        }.get(frequency, self.segment_active)
        target.add(stage)
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
            return context.get("task_complexity") in {"standard", "complex"}
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

    def on_task_start(self, context: Optional[Mapping[str, Any]] = None) -> None:
        """Dispatch declared activation, not an implicit execution mode.

        ``execution_mode`` describes how a stage behaves once active. Whether
        an event activates it is declared separately by ``activation_predicates``
        (or by the preflight convention). Conflating the two made
        ``anchor_open`` — declared ``state`` + ``task_start`` — never activate.
        """
        payload = dict(context or {})
        for stage, spec in self.specs.items():
            predicates = spec.get("activation_predicates") or []
            if predicates:
                self.activate_predicates(stage, "task_start", payload)
            elif spec.get("execution_mode") == "preflight":
                self.activate(stage, "task_start")

    def reset_for_segment(self) -> None:
        """Start a fresh model subcall without re-running task activation.

        Re-injecting context into a new segment is not re-executing the task.
        Task-scoped activation and counts survive segment boundaries; only
        per-action/per-response visibility is rebuilt.
        """
        # Reinjection is not re-execution: task-scoped activation persists;
        # ephemeral stages belong only to their current boundary.
        self.segment_active = {stage for stage in self.task_active
                              if self.specs.get(stage, {}).get("frequency") == "per_task"}
        self.action_active.clear()
        self.artifact_active.clear()
        self.response_active.clear()

    def on_model_call(self, *, tool_capable: bool = False,
                      action_hint: Optional[ActionDescriptor] = None,
                      task_complexity: Optional[str] = None) -> None:
        """Boundary before model generation.

        Model capability is not action intent. Explicit host dispatch through
        on_tool_planning activates action wrappers when a tool decision begins.
        """
        self.on_task_start({"task_complexity": task_complexity})

    def on_tool_planning(self, descriptor: Optional[ActionDescriptor] = None) -> None:
        """Expose temporary action guidance only at an explicit planning boundary."""
        self.action_active.clear()
        self.activate_predicates("fuse", "action", {
            "mutability": "read", "action_planning": True})
        if descriptor is not None:
            self.activate_predicates("ward", "action", descriptor.__dict__)


    def on_action(self, decision: GateDecision,
                  descriptor: Optional[ActionDescriptor] = None) -> None:
        context = descriptor.__dict__ if descriptor is not None else {}
        self.activate_predicates("fuse", "action", context)
        self.activate_predicates("ward", "action", context)
        if decision != GateDecision.ALLOW:
            self.activate("ward", "action_gate")

    def on_action_complete(self) -> None:
        self.action_active.clear()

    def on_artifact(self, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True) -> None:
        self.artifact_active.clear()
        if flow_triggered:
            self.activate("flow", "artifact_trigger")
        if durable_contract_changed:
            self.activate("dox_closeout", "durable_contract")
        if state_changed:
            self.activate("anchor_closeout", "artifact_state")

    def on_artifact_processed(self) -> None:
        self.artifact_active.clear()

    def on_completion(self) -> None:
        self.response_active.clear()
        self.activate("sispis", "completion")

    def on_response_complete(self) -> None:
        self.response_active.clear()


class HostTelemetry:
    """Wires the host runtime into the DP event plane without importing DP
    code: emits schema-shaped events to a per-task NDJSON store (0600). The
    resolver/doctor must never import DigitalPsychology; this adapter only
    writes JSON lines in the DP event schema."""

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
        schema_path = LOCAL_CONTRACT_ROOT / "behavior-event.schema.json"
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
        global _BEHAVIOR_SCHEMA_VALIDATOR
        try:
            import jsonschema
            from jsonschema import Draft202012Validator, FormatChecker
            schema_path = LOCAL_CONTRACT_ROOT / "behavior-event.schema.json"
            if _BEHAVIOR_SCHEMA_VALIDATOR is None:
                if not schema_path.exists():
                    raise ValueError("behavior-event schema is unavailable")
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
                   "namespace_id", "application_id", "application_instance_id",
                   "agent_id", "actor_id", "agent_instance_id", "session_id",
                   "attempt_id", "segment_id", "behavioral_subject", *OPTIONAL_EVENT_FIELDS}
        forbidden = set(event) - allowed
        if forbidden:
            raise ValueError(f"telemetry envelope has forbidden fields: {sorted(forbidden)}")
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

    def record_statework_evidence(self, evidence_type: str, *, subject_ref: str,
                                  content: Mapping[str, Any]) -> str:
        return self._session._record_statework_evidence(
            self._capability, evidence_type, subject_ref=subject_ref, content=content)

    def record_blocker(self, blocker_id: str, summary: str, *, evidence_ref: str,
                       criterion_ids: Iterable[str] = ()) -> None:
        self._session._record_blocker(
            self._capability, blocker_id=blocker_id, summary=summary,
            evidence_ref=evidence_ref, criterion_ids=criterion_ids)

    def clear_blocker(self, blocker_id: str, *, evidence_ref: str) -> None:
        self._session._clear_blocker(
            self._capability, blocker_id=blocker_id, evidence_ref=evidence_ref)

    def record_tool_result(self, tool_type: str, result_class: str,
                           subject_ref: Optional[str] = None,
                           *, invocation_id: Optional[str] = None,
                           result_digest: Optional[str] = None,
                           action_request_id: Optional[str] = None,
                           actual_result: Optional[Mapping[str, Any]] = None) -> str:
        return self._session._record_tool_result(
            self._capability, tool_type, result_class, subject_ref,
            invocation_id=invocation_id, result_digest=result_digest,
            action_request_id=action_request_id,
            actual_result=actual_result)

    def record_tool_outcome(self, tool_type: str, result_class: str,
                            subject_ref: Optional[str] = None, **kwargs: Any) -> str:
        """Explicit alias matching the host interceptor contract."""
        return self.record_tool_result(tool_type, result_class, subject_ref, **kwargs)

    def complete_action(self) -> None:
        self._session._complete_action(self._capability)

    def begin_attempt(self, *, attempt_ref: str, cause_id: str,
                      premise_digest: str, action_plan: Mapping[str, Any],
                      parent_attempt_ref: Optional[str] = None,
                      semantic_descriptor: Optional[Mapping[str, Any]] = None) -> AttemptDescriptor:
        return self._session._begin_attempt(
            self._capability, attempt_ref=attempt_ref, cause_id=cause_id,
            premise_digest=premise_digest, action_plan=action_plan,
            parent_attempt_ref=parent_attempt_ref,
            semantic_descriptor=semantic_descriptor)

    def record_attempt_failure(self, *, attempt_ref: str,
                               evidence_ref: str) -> None:
        self._session._record_attempt_failure(
            self._capability, attempt_ref=attempt_ref, evidence_ref=evidence_ref)

    def record_reinspection(self, *, cause_id: str, revision: int) -> None:
        self._session._record_reinspection(
            self._capability, cause_id=cause_id, revision=revision)

    def record_goal_progress(self, *, cause_id: str, evidence_ref: str,
                             criterion_id: Optional[str] = None,
                             progress_class: str = "GOAL_PROGRESS") -> None:
        self._session._record_goal_progress(
            self._capability, cause_id=cause_id, evidence_ref=evidence_ref,
            criterion_id=criterion_id, progress_class=progress_class)

    def system_one_decision_for_action(self, action_request_id: str) -> Optional[str]:
        """Host-only correlation lookup for a later independent outcome."""
        return self._session._system_one_decision_for_action(
            self._capability, action_request_id)

    def record_system_one_outcome(self, *, decision_id: str, label: str,
                                  value: str, evidence_ref: str,
                                  authority: str,
                                  criterion_id: Optional[str] = None) -> str:
        return self._session._record_system_one_outcome(
            self._capability, decision_id=decision_id, label=label, value=value,
            evidence_ref=evidence_ref, authority=authority,
            criterion_id=criterion_id)

    def rank_legal_routes(
            self, legal_children: Mapping[str, resolve_runtime.RuntimeBundle], *,
            state_revision: int, target_statework_id: str,
            route_preference: Optional[str] = None,
            preference_source: str = "metacognition") -> Optional[RouteReplanRecommendation]:
        return self._session._rank_legal_routes(
            self._capability, legal_children=legal_children,
            state_revision=state_revision, target_statework_id=target_statework_id,
            route_preference=route_preference, preference_source=preference_source)

    def create_child_policy_epoch(
            self, recommendation: RouteReplanRecommendation, *,
            selected_route: str, current_state_revision: int) -> ChildPolicyEpoch:
        return self._session._create_child_policy_epoch(
            self._capability, recommendation, selected_route=selected_route,
            current_state_revision=current_state_revision)

    def record_recovery_epoch(self, *, cause_id: str, reason: str) -> None:
        self._session._record_recovery_epoch(
            self._capability, cause_id=cause_id, reason=reason)

    def reject_investigative_path(self, *, attempt_ref: str,
                                  evidence_ref: str) -> None:
        self._session._reject_investigative_path(
            self._capability, attempt_ref=attempt_ref, evidence_ref=evidence_ref)

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

    def record_authorization(self, authorization: Mapping[str, Any],
                             subject_ref: Optional[str] = None) -> str:
        return self._session._record_authorization(
            self._capability, authorization, subject_ref=subject_ref)

    def record_repository_truth(self, packet: Mapping[str, Any],
                                observation_context: Mapping[str, Any],
                                subject_ref: Optional[str] = None) -> str:
        return self._session._record_repository_truth(
            self._capability, packet, observation_context, subject_ref=subject_ref)

    def record_repository_evidence(self, packet: Mapping[str, Any],
                                   validation: Mapping[str, Any],
                                   subject_ref: Optional[str] = None) -> str:
        return self._session._record_repository_evidence(
            self._capability, packet, validation, subject_ref=subject_ref)

    def record_rollback_plan(self, plan: Mapping[str, Any],
                             subject_ref: Optional[str] = None) -> str:
        return self._session._record_typed_authority(
            self._capability, "rollback_plan", "host:rollback-plan",
            "rollback-plan-v1", "rollback-plan-v1", plan, subject_ref)

    def record_rollback(self, result: Mapping[str, Any],
                        subject_ref: Optional[str] = None) -> str:
        return self._session._record_typed_authority(
            self._capability, "rollback", "host:rollback", "rollback-v1",
            "rollback-record-v1", result, subject_ref)

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

    def request_termination(self, *, disposition: str, reason: str, cause_id: str,
                           evidence_refs: Iterable[str] = (),
                           last_verified_state: Optional[str] = None,
                           unresolved_blockers: Iterable[str] = ()) -> TerminationPermit:
        return self._session.request_termination(
            self._capability, disposition=disposition, reason=reason, cause_id=cause_id,
            evidence_refs=evidence_refs, last_verified_state=last_verified_state,
            unresolved_blockers=unresolved_blockers)

    def finish_task_terminated(self, output: Optional[Dict[str, Any]] = None,
                               *, permit: Optional[TerminationPermit] = None) -> Dict[str, Any]:
        return self._session.finish_task_terminated(output, permit=permit)

    def publish_handoff(self, packet: Dict[str, Any], *,
                        observation_context: Optional[Mapping[str, Any]] = None) -> str:
        return self._session._publish_handoff(
            self._capability, packet, observation_context=observation_context)


class StateAuthority:
    """Host interface for combined CSW authority; the session never owns it."""
    def inspect(self, *, statework_id: str, subject_ref: str, contract_hash: str) -> Dict[str, Any]:
        raise NotImplementedError
    def commit(self, *, statework_id: str, subject_ref: str, contract_hash: str,
               expected_revision: int, output_state: str, evidence_refs: list[str], trigger: str,
               evidence_records: Optional[list[Mapping[str, Any]]] = None) -> Dict[str, Any]:
        raise NotImplementedError


class PolicyRevocationAuthority:
    """Host interface for global profile rollback/revocation revisions."""
    def inspect(self, profile_hashes: tuple[str, ...]) -> dict[str, Any]:
        raise NotImplementedError


class SharedWorkflowMembershipAuthority:
    """Trusted host issuer/validator for shared workflow membership grants."""
    def __init__(self, signing_key: bytes) -> None:
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("membership authority requires at least 256 bits of key material")
        self._key = signing_key
        self._grants: Dict[str, Dict[str, Any]] = {}
        self._revocations: set[str] = set()

    @staticmethod
    def _grant_id(membership: Mapping[str, Any]) -> str:
        return hashlib.sha256(json.dumps(dict(membership), sort_keys=True,
                                           separators=(",", ":"), default=str).encode()).hexdigest()

    def issue(self, *, namespace_id: str, application_id: str,
              application_instance_id: str, workflow_id: str, statework_id: str,
              subject_ref: str, member_agent_id: str,
              allowed_operations: Iterable[str], expires_at: str,
              membership_revision: int) -> Dict[str, Any]:
        grant = {
            "schema_version": "1.0.0", "namespace_id": namespace_id,
            "application_id": application_id, "application_instance_id": application_instance_id,
            "workflow_id": workflow_id, "statework_id": statework_id,
            "subject_ref": subject_ref, "member_agent_id": member_agent_id,
            "allowed_operations": tuple(sorted(set(allowed_operations))),
            "expires_at": expires_at, "membership_revision": int(membership_revision),
        }
        grant["grant_id"] = self._grant_id(grant)
        grant["signature"] = hmac.new(
            self._key, json.dumps(grant, sort_keys=True, separators=(",", ":"),
                                  default=str).encode(), hashlib.sha256).hexdigest()
        self._grants[grant["grant_id"]] = grant
        return dict(grant)

    def revoke(self, grant_id: str) -> None:
        if grant_id not in self._grants:
            raise KeyError(grant_id)
        self._revocations.add(grant_id)

    def validate(self, membership: Mapping[str, Any], *, session: "RuntimeSession",
                 operation: str) -> bool:
        grant = dict(membership)
        signature = str(grant.pop("signature", ""))
        grant_id = str(grant.get("grant_id") or "")
        if grant_id not in self._grants or grant_id in self._revocations:
            return False
        expected = hmac.new(
            self._key, json.dumps(grant, sort_keys=True, separators=(",", ":"),
                                  default=str).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        if grant.get("member_agent_id") != session.agent_instance_id:
            return False
        if operation not in set(grant.get("allowed_operations") or ()):
            return False
        try:
            expiry = datetime.fromisoformat(str(grant.get("expires_at")).replace("Z", "+00:00"))
        except ValueError:
            return False
        return expiry > datetime.now(timezone.utc)


class CSWStateAuthority(StateAuthority):
    """Concrete combined-mode adapter owned by CSW, injected into CFW."""
    def __init__(self, root: Path, *, state_path: Optional[Path] = None,
                 evidence_path: Optional[Path] = None,
                 namespace_id: str = "default", application_id: str = "cognitiveframeworks-runtime",
                 application_instance_id: str = "cognitiveframeworks-runtime",
                 workflow_id: str = "default-workflow") -> None:
        self.root = Path(root)
        self.control = _statework_transition_engines(self.root)
        self.state_store = self.control.SubjectStateStore(
            Path(state_path) if state_path else self.root / "state" / "subjects.json")
        self.evidence_store = self.control.EvidenceStore()
        self.evidence_path = Path(evidence_path) if evidence_path else self.root / "state" / "evidence.json"
        object.__setattr__(self, "namespace_id", str(namespace_id))
        object.__setattr__(self, "application_id", str(application_id))
        object.__setattr__(self, "application_instance_id", str(application_instance_id))
        object.__setattr__(self, "workflow_id", str(workflow_id))
        object.__setattr__(self, "_frozen_scope", True)
        self._controllers: Dict[str, Any] = {}
        self._load_evidence()

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_frozen_scope", False) and name in {
                "namespace_id", "application_id", "application_instance_id", "workflow_id"}:
            raise AttributeError("CSW authority scope is immutable")
        object.__setattr__(self, name, value)

    def bind_scope(self, *, namespace_id: str, application_id: str,
                   application_instance_id: str, workflow_id: str) -> "CSWStateAuthority":
        """Return a task-scoped view while sharing durable storage services."""
        return CSWStateAuthority(
            self.root, state_path=self.state_store.path,
            evidence_path=self.evidence_path, namespace_id=namespace_id,
            application_id=application_id, application_instance_id=application_instance_id,
            workflow_id=workflow_id)

    def _load_evidence(self) -> None:
        if not self.evidence_path.exists():
            return
        try:
            records = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("CSW evidence authority is unreadable") from exc
        if isinstance(records, list):
            self.evidence_store = self.control.EvidenceStore.load_attested(records)

    def _contract(self, statework_id: str, contract_hash: str) -> tuple[Dict[str, Any], str]:
        import yaml
        path = self.root / statework_id / "transitions.yaml"
        if not path.is_file():
            raise ValueError(f"CSW StateWork {statework_id!r} is unavailable")
        contract = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        actual = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        if actual != contract_hash:
            raise ValueError("CFW pinned contract does not match CSW authority contract")
        return contract, actual

    def _identity(self, *, statework_id: str, subject_ref: str, contract: Mapping[str, Any]) -> Dict[str, Any]:
        return {"namespace_id": self.namespace_id, "application_id": self.application_id,
                "application_instance_id": self.application_instance_id,
                "workflow_id": self.workflow_id,
                "subject_ref": subject_ref, "statework_id": statework_id,
                "contract_version": str(contract.get("version") or "1.0.0")}

    def inspect(self, *, statework_id: str, subject_ref: str, contract_hash: str) -> Dict[str, Any]:
        contract, _ = self._contract(statework_id, contract_hash)
        identity = self._identity(statework_id=statework_id, subject_ref=subject_ref, contract=contract)
        key = self.control.SubjectStateStore.key(identity)
        if key not in self._controllers:
            registry = self.control.load_evidence_kind_registry(self.root / "schemas" / "evidence-kinds.yaml")
            self._controllers[key] = self.control.StandaloneStateWorkController(
                contract=contract, identity=identity, state_store=self.state_store,
                evidence_store=self.evidence_store, evidence_registry=registry)
        return self._controllers[key].inspect()

    def commit(self, *, statework_id: str, subject_ref: str, contract_hash: str,
               expected_revision: int, output_state: str, evidence_refs: list[str], trigger: str,
               evidence_records: Optional[list[Mapping[str, Any]]] = None) -> Dict[str, Any]:
        contract, _ = self._contract(statework_id, contract_hash)
        identity = self._identity(statework_id=statework_id, subject_ref=subject_ref, contract=contract)
        key = self.control.SubjectStateStore.key(identity)
        controller = self._controllers.get(key)
        if controller is None:
            self.inspect(statework_id=statework_id, subject_ref=subject_ref, contract_hash=contract_hash)
            controller = self._controllers[key]
        scoped_refs: list[str] = []
        for record in (evidence_records or []):
            copied = dict(record)
            original_ref = str(copied.get("ref") or "")
            if not original_ref:
                raise ValueError("CSW evidence record requires a ref")
            scoped_ref = "evidence:" + hashlib.sha256(
                (f"{identity['namespace_id']}|{identity['application_id']}|"
                 f"{identity['application_instance_id']}|{identity['workflow_id']}|"
                 f"{statework_id}|{subject_ref}|{original_ref}").encode("utf-8")
            ).hexdigest()
            copied["ref"] = scoped_ref
            copied["origin_ref"] = original_ref
            copied["namespace_id"] = identity["namespace_id"]
            copied["application_id"] = identity["application_id"]
            copied["application_instance_id"] = identity["application_instance_id"]
            copied["workflow_id"] = identity["workflow_id"]
            self.evidence_store.create(copied)
            scoped_refs.append(scoped_ref)
        if scoped_refs:
            # Evidence must be durable before a state transition can reference it.
            self.evidence_store.save(self.evidence_path)
        decision = controller.request_transition(
            to_state=output_state, trigger=trigger, evidence_refs=scoped_refs,
            expected_revision=expected_revision)
        if not decision.allowed:
            raise ValueError(f"CSW authority blocked transition: {decision.reason}")
        return {**controller.inspect(), "transition": decision.progress_facts()}


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
    _telemetry_exporter: Optional[DurableTelemetryExporter] = None
    state_authority: Optional[StateAuthority] = None
    policy_revocation_authority: Optional[PolicyRevocationAuthority] = None
    membership_authority: Optional[SharedWorkflowMembershipAuthority] = None
    operator_read_model: Optional[Any] = None
    convergence: Optional[ConvergenceController] = None
    termination_disposition: str = "RUNNING"
    termination_permit: Optional[Any] = None
    _operator_sequence: int = 0
    runtime_mode: str = "standalone_enforcing"
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
    parent_decision_ref: Optional[str] = None
    shared_evidence_refs: tuple[str, ...] = ()
    delegation_topology: Optional[str] = None
    premise_digest: Optional[str] = None
    failure_attribution: str = "none"
    adopted_profile_hashes: tuple[str, ...] = ()
    execution_identity: Optional[ExecutionIdentity] = None
    shared_workflow_membership: Optional[Mapping[str, Any]] = None
    policy_revocation_revision: int = 0
    revocation_mode: str = "deferred"
    authority_revision: int = 0
    _completion_permit: Optional[CompletionPermit] = field(default=None, repr=False)
    _security_frozen: bool = field(default=False, init=False, repr=False)
    journal_path: Optional[Path] = None
    journal_db_path: Optional[Path] = None
    _host_capability: _HostCapability = field(default_factory=_HostCapability,
                                               init=False, repr=False, compare=False)
    _ledger_token: Any = field(default_factory=object, init=False, repr=False, compare=False)
    route_decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    route_outcomes: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    attempt_descriptors: Dict[str, AttemptDescriptor] = field(default_factory=dict, repr=False)
    attempt_semantics: Dict[str, AttemptSemanticDescriptor] = field(default_factory=dict, repr=False)
    attempt_failures: set[str] = field(default_factory=set, repr=False)
    _semantic_retry_same: set[str] = field(default_factory=set, repr=False)
    shared_branch_ledger: Optional[SharedBranchLedger] = field(default=None, repr=False)
    system_one_judge: Optional[BehavioralJudge] = field(default=None, repr=False)
    metacognitive_state: MetacognitiveState = field(
        default_factory=MetacognitiveState, repr=False)
    system_one_projector: SystemOneStateProjector = field(
        default_factory=SystemOneStateProjector, repr=False)
    metacognitive_controller: MetacognitiveController = field(
        default_factory=MetacognitiveController, repr=False)
    semantic_signals: SemanticSignals = field(default_factory=SemanticSignals, repr=False)
    semantic_signals_revision: int = field(default=-1, repr=False)
    metacognitive_decision: MetaDecision = field(
        default_factory=lambda: MetaDecision("DELIVER", "task initialized"), repr=False)
    system_one_sequence: int = 0
    system_one_decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    system_one_outcomes: set[str] = field(default_factory=set, repr=False)
    _pending_action_requests: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    _pending_retry_candidates: Dict[str, ActivityFact] = field(default_factory=dict, repr=False)
    policy_epoch_record: Optional[PolicyEpochRecord] = field(default=None, repr=False)
    _system_one_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _system_one_provider_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)

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
                "delegate_agent_id", "delegation_id", "role", "parent_decision_ref",
                "shared_evidence_refs", "delegation_topology", "premise_digest", "failure_attribution",
                "adopted_profile_hashes", "execution_identity", "shared_workflow_membership", "policy_revocation_revision", "revocation_mode",
                "policy_revocation_authority", "membership_authority", "operator_read_model",
                "convergence",
                "route_outcomes", "journal_path", "journal_db_path", "_telemetry_exporter", "state_authority",
                "runtime_mode", "attempt_descriptors", "attempt_failures",
                "attempt_semantics", "_semantic_retry_same",
                "shared_branch_ledger", "system_one_judge", "metacognitive_state",
                "system_one_projector", "metacognitive_controller", "semantic_signals",
                "semantic_signals_revision",
                "metacognitive_decision",
                "system_one_sequence", "system_one_decisions", "system_one_outcomes",
        "_pending_action_requests", "_pending_retry_candidates", "policy_epoch_record"}:
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

    def _action_strategy(self) -> Dict[str, Any]:
        """Return a bounded, advisory action plan inside host-owned policy.

        A learned preference may order already-authorized bindings, but this
        object grants no permission; ``before_action`` remains authoritative.
        """
        preferences = []
        for adjustment in self.bundle.guard_pack.get("routing_adjustments", ()):
            if (adjustment.get("route_type") == "tool_policy"
                    and adjustment.get("disposition") == "prefer"):
                route = str(adjustment.get("route") or "")
                if route in self.action_registry:
                    preferences.append(route)
        eligible = [route for route in self.action_registry if route not in preferences]
        return ActionStrategy(
            eligible_actions=tuple(preferences + eligible),
            priorities={route: index for index, route in enumerate(preferences)},
            reason="learned preference within host-authorized action set",
            policy_hash=str(self.bundle.pinned.get("policy_hash") or ""),
        ).to_dict()

    def _action_strategy_hint(self) -> str:
        strategy = self._action_strategy()
        if not strategy["eligible_actions"]:
            return ""
        preferred = [action for action, _priority in sorted(
            strategy["priorities"].items(), key=lambda pair: pair[1])]
        if not preferred:
            return ""
        other = [action for action in strategy["eligible_actions"] if action not in preferred]
        lines = ["\n===== ACTION STRATEGY =====",
                 f"Preferred when relevant: {', '.join(preferred[:12])}."]
        if other:
            lines.append(f"Other authorized actions: {', '.join(other[:12])}.")
        lines.append("Preferences do not grant permission; every action still passes the host gate.")
        return "\n".join(lines)

    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Optional[Mapping[str, Any]] = None) -> str:
        if self.lifecycle is not None:
            prior = set(self.active_lifecycle)
            descriptor = self._descriptor(action_hint) if action_hint is not None else None
            complexity = (self.bundle.pinned.get("goal_contract") or {}).get(
                "task_complexity", "standard" if self.bundle.profile != "quick" else "trivial")
            self.lifecycle.on_model_call(tool_capable=tool_capable,
                                         action_hint=descriptor,
                                         task_complexity=complexity)
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

    def artifact_processed(self) -> None:
        if self.lifecycle is not None:
            self.lifecycle.on_artifact_processed()
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)

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
        for key in ("namespace_id", "application_id", "application_instance_id",
                    "application_version", "provider_id", "model_id", "model_revision",
                    "model_capability_hash", "harness_id", "harness_version"):
            if key in self.bundle.pinned and self.bundle.pinned.get(key) is not None:
                event.setdefault(key, self.bundle.pinned[key])
        event.setdefault("goal_contract_hash",
                         self.bundle.pinned.get("goal_contract_hash") or None)
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
        if self.operator_read_model is not None:
            try:
                self._operator_sequence = self.operator_read_model.allocate_sequence()
                self.operator_read_model.publish(OperatorEvent.create(
                    sequence=self._operator_sequence,
                    namespace_id=str(event.get("namespace_id") or "default"),
                    application_instance_id=str(event.get("application_instance_id") or "unknown"),
                    agent_instance_id=event.get("agent_instance_id"),
                    session_id=event.get("session_id"), task_id=self.bundle.task_id,
                    interaction_id=event.get("interaction_id"),
                    workflow_id=(self.shared_workflow_membership or {}).get("workflow_id"),
                    event_type=str(event.get("event_type") or "runtime_event"),
                    severity=("warning" if "block" in str(event.get("event_type") or "") else "info"),
                    summary=str((event.get("payload") or {}).get("summary") or event.get("event_type") or "Runtime event")[:512],
                    reason_code=(event.get("payload") or {}).get("result_class"),
                    source_system="cfw", provenance="host_verified",
                    correlation_id=f"{self.session_id}:{self.bundle.task_id}",
                    evidence_refs=tuple(event.get("evidence_refs") or ())))
            except Exception:
                pass
        if self._emit:
            self._emit(event)
        if self._telemetry_exporter is not None:
            self._telemetry_exporter.enqueue()

    def _system_one_policy(self) -> SystemOnePolicy:
        return SystemOnePolicy(**dict(self.bundle.pinned.get("system_one_policy") or {}))

    def _evaluate_metacognition(self, *,
                                 semantic: Optional[SemanticSignals] = None) -> MetaDecision:
        goal = self.bundle.pinned.get("goal_contract") or {}
        verification = goal.get("verification_policy") or {}
        state = self.convergence.state("primary-objective") if self.convergence else None
        decision = self.metacognitive_controller.evaluate(
            self.metacognitive_state,
            support_actions_since_progress=(
                state.support_actions_since_goal_progress if state else 0),
            support_action_limit=int(verification.get(
                "support_actions_before_goal_reanchor", 5) or 5),
            recovery_epochs_since_progress=(
                state.recovery_epochs_since_goal_progress if state else 0),
            max_recovery_epochs=int(verification.get(
                "max_recovery_epochs_without_goal_progress", 3) or 3),
            same_approach_retries=(state.same_approach_retries if state else 0),
            max_same_approach_retries=int(verification.get(
                "max_same_approach_retries", 2) or 2),
            unresolved_contradiction=any(
                not bool(item.get("resolved")) for item in self.contradictions.values()),
            semantic=(semantic if semantic is not None else
                      self.semantic_signals
                      if self.semantic_signals_revision == self.metacognitive_state.logical_revision
                      else SemanticSignals()))
        if decision != self.metacognitive_decision:
            object.__setattr__(self, "metacognitive_decision", decision)
            self._journal("metacognitive_decision_recorded", decision=decision.to_dict(),
                          logical_revision=self.metacognitive_state.logical_revision)
        return decision

    def _system_one_query(self, *, questions: tuple[str, ...], candidate: ActivityFact,
                          subject_ref: Optional[str] = None,
                          action_request_id: Optional[str] = None,
                          action_invocation_id: Optional[str] = None,
                          descriptor: Optional[ActionDescriptor] = None,
                          route_candidates: tuple[str, ...] = (),
                          route_state_revision: Optional[int] = None,
                          route_statework_id: Optional[str] = None) -> Optional[SystemOneJudgment]:
        """Serialize a complete provider operation for this RuntimeSession identity."""
        with self._system_one_provider_lock:
            return self._system_one_query_serialized(
                questions=questions, candidate=candidate, subject_ref=subject_ref,
                action_request_id=action_request_id,
                action_invocation_id=action_invocation_id, descriptor=descriptor,
                route_candidates=route_candidates,
                route_state_revision=route_state_revision,
                route_statework_id=route_statework_id)

    def _system_one_query_serialized(self, *, questions: tuple[str, ...], candidate: ActivityFact,
                          subject_ref: Optional[str] = None,
                          action_request_id: Optional[str] = None,
                          action_invocation_id: Optional[str] = None,
                          descriptor: Optional[ActionDescriptor] = None,
                          route_candidates: tuple[str, ...] = (),
                          route_state_revision: Optional[int] = None,
                          route_statework_id: Optional[str] = None) -> Optional[SystemOneJudgment]:
        """Query from a typed host snapshot after authority checks."""
        policy = self._system_one_policy()
        if policy.mode == "disabled":
            return None
        enabled_questions = set(policy.enabled_question_families)
        questions = tuple(question for question in questions
                          if question in enabled_questions)
        if not questions:
            return None
        goal = self.bundle.pinned.get("goal_contract") or {}
        if not goal.get("objective") or self.system_one_judge is None:
            return None
        with self._system_one_lock:
            object.__setattr__(self, "system_one_sequence", self.system_one_sequence + 1)
            sequence = self.system_one_sequence
        self._journal("system_one_sequence", sequence=sequence)
        identity = SystemOneIdentity(
            namespace_id=self.execution_identity.namespace_id,
            application_id=str(self.bundle.pinned.get("application_id") or "unknown-application"),
            application_instance_id=self.execution_identity.application_instance_id,
            agent_id=str(self.bundle.pinned.get("agent_id") or "agent"),
            agent_instance_id=self.execution_identity.agent_instance_id,
            session_id=self.execution_identity.session_id,
            task_id=self.execution_identity.task_id,
            attempt_id=self.execution_identity.attempt_id,
            interaction_id=self.execution_identity.interaction_id)
        convergence_state = self.convergence.state("primary-objective") if self.convergence else None
        convergence = {
            "support_actions_since_goal_progress": int(
                convergence_state.support_actions_since_goal_progress if convergence_state else 0),
            "recovery_epochs_since_goal_progress": int(
                convergence_state.recovery_epochs_since_goal_progress if convergence_state else 0),
            "same_approach_retries": int(
                convergence_state.same_approach_retries if convergence_state else 0),
        }
        active_flow = None
        active_statework_id = self.current_segment.statework_id
        if active_statework_id:
            active_statework = next((item for item in self.bundle.stateworks
                                     if item.get("id") == active_statework_id), None)
            selected_flow = active_statework.get("selected_flow") if active_statework else None
            if isinstance(selected_flow, (list, tuple)) and selected_flow:
                active_flow = Path(str(selected_flow[0])).parent.name
        query_meta_revision = self.metacognitive_state.logical_revision
        frame = self.system_one_projector.project(
            meta_state=self.metacognitive_state,
            identity=identity, sequence=sequence,
            policy_hash=str(self.bundle.pinned.get("policy_hash") or ""),
            policy=policy, questions=questions, goal_contract=goal,
            convergence=convergence, candidate=candidate,
            phase=self.bundle.pinned.get("phase"),
            active_segment=self.current_segment.segment_id,
            active_statework=(active_statework_id or None), active_flow=active_flow,
            current_state=(self.statework_states.get((
                active_statework_id or "", self.current_segment.subject_ref or ""))),
            blockers=tuple(f"{item.blocker_id}: {item.summary}" if item.summary
                           else item.blocker_id for item in self.metacognitive_state.blockers),
            failed_approaches=tuple(
                self.attempt_semantics[attempt_ref]
                for attempt_ref in self.attempt_descriptors
                if attempt_ref in self.attempt_failures
                and attempt_ref in self.attempt_semantics)[-4:],
            legal_routes=route_candidates)
        status_reason = "fallback"
        calibrated_questions: set[str] = set()
        judgment: Optional[SystemOneJudgment] = None
        try:
            # This lock is per RuntimeSession identity: stateful providers see
            # each full snapshot and its answer validation as one operation.
            with self._system_one_provider_lock:
                raw = self.system_one_judge.judge(frame, mode=policy.mode)
                if hasattr(raw, "judgment"):
                    status_reason = str(
                        getattr(raw, "reason", None)
                        or getattr(raw, "status", "fallback"))[:64]
                    calibration_hint = bool(getattr(raw, "calibrated", False))
                    calibrated_questions.update(
                        getattr(raw, "calibrated_question_ids", ()) or ())
                    raw = getattr(raw, "judgment", None)
                else:
                    calibration_hint = False
                if raw is not None:
                    judgment = (raw if isinstance(raw, SystemOneJudgment)
                                else SystemOneJudgment.from_mapping(
                                    raw.to_dict() if hasattr(raw, "to_dict") else raw))
                    self._validate_system_one_judgment(frame, judgment)
                    if calibration_hint and not calibrated_questions:
                        calibrated_questions.update(frame.question_ids)
                    calibrated_questions = {
                        question_id for question_id in calibrated_questions
                        if question_id in frame.question_ids
                        and (answer := judgment.answer(question_id)) is not None
                        and answer.calibrated_probabilities is not None
                        and answer.calibration_hash is not None
                    }
        except Exception:
            judgment, calibrated_questions = None, set()
            status_reason = "provider_exception"
        if (sequence != self.system_one_sequence
                or query_meta_revision != self.metacognitive_state.logical_revision):
            judgment, calibrated_questions = None, set()
            status_reason = "stale_response"
        status = _canonical_system_one_status(
            status_reason, has_judgment=judgment is not None, mode=policy.mode)
        calibrated = frozenset(calibrated_questions)

        relation = None
        influenced = False
        if descriptor is not None and "action_relation" in questions:
            fused = fuse_action_relation(
                descriptor, judgment=judgment, policy=policy, calibrated=calibrated)
            relation = fused.relation
            influenced = fused.model_influenced
        signals = SemanticFusion.signals(
            judgment, policy=policy, calibrated=calibrated)
        prior_decision = self.metacognitive_decision
        object.__setattr__(self, "semantic_signals", signals)
        object.__setattr__(self, "semantic_signals_revision",
                           query_meta_revision)
        next_decision = self._evaluate_metacognition()
        control_changed = (next_decision.mode, next_decision.reason,
                           next_decision.goal_reanchor_required) != (
                               prior_decision.mode, prior_decision.reason,
                               prior_decision.goal_reanchor_required)
        semantic_influenced = bool(next_decision.semantic_attention_note)

        question_hash = frame.question_bundle_digest
        decision_id = frame.request_id
        answers = ({answer.question_id: answer.to_dict()
                    for answer in judgment.answers} if judgment else {})
        decision = {
            "decision_id": decision_id, "subject_ref": subject_ref,
            "action_request_id": action_request_id,
            "action_invocation_id": action_invocation_id,
            "sequence": frame.sequence, "state_digest": frame.state_digest,
            "request_digest": frame.request_digest,
            "policy_hash": frame.policy_hash,
            "legal_routes": list(route_candidates),
            "route_state_revision": route_state_revision,
            "route_statework_id": route_statework_id,
            "question_hash": question_hash,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "judgment": judgment.to_dict() if judgment else {},
            "answers": answers, "status": status,
            "status_reason": status_reason,
            "calibrated": bool(calibrated),
            "calibrated_question_ids": sorted(calibrated),
            "influenced": influenced,
            "semantic_signals": signals.to_dict(),
            "metacognitive_decision": next_decision.to_dict(),
            "system_one_semantic_influenced": semantic_influenced,
            "metacognitive_control_changed": control_changed,
            "control_change_source": ("deterministic_metacognition"
                                      if control_changed else None),
            "fused_action_relation": relation,
        }
        self.system_one_decisions[decision_id] = decision
        while len(self.system_one_decisions) > 512:
            oldest = next(iter(self.system_one_decisions))
            self.system_one_decisions.pop(oldest)
            self.system_one_outcomes.difference_update(
                key for key in tuple(self.system_one_outcomes)
                if key.startswith(f"{oldest}:"))
        self._journal("system_one_decision_recorded", decision=decision)
        payload = {
            "summary": "bounded System-1 semantic judgment" if judgment else "System-1 fallback",
            "system_one_decision_id": decision_id,
            "system_one_mode": policy.mode,
            "system_one_backend_id": policy.backend_id,
            "system_one_model_id": policy.model_id,
            "system_one_model_revision": policy.model_revision,
            "system_one_model_content_hash": policy.model_content_hash,
            "system_one_provider_protocol_hash": policy.provider_protocol_hash,
            "system_one_protocol_version": policy.protocol_version,
            "system_one_protocol_hash": policy.protocol_hash,
            "system_one_question_registry_hash": policy.question_registry_hash,
            "system_one_state_projection_schema_hash": policy.state_projection_schema_hash,
            "system_one_calibration_hash": policy.calibration_bundle_hash,
            "system_one_work_mode": str(goal.get("work_mode") or "delivery"),
            "system_one_deliverable_kind": str(goal.get("deliverable_kind") or "code_behavior"),
            "system_one_sequence": frame.sequence,
            "system_one_state_digest": frame.state_digest,
            "system_one_question_bundle_hash": question_hash,
            "system_one_status": status,
            "system_one_status_reason": status_reason,
            "system_one_influenced": influenced,
            "system_one_semantic_influenced": semantic_influenced,
            "metacognitive_control_changed": control_changed,
            "control_change_source": ("deterministic_metacognition"
                                      if control_changed else None),
            "source_decision_ids": list(signals.source_decisions),
            "system_one_answers": answers,
        }
        if action_request_id:
            payload["action_request_id"] = action_request_id
        if action_invocation_id:
            payload["action_invocation_id"] = action_invocation_id
        if relation is not None:
            payload["system_one_fused_action_relation"] = relation
        if judgment is not None:
            event_labels = {
                "action_relation": "system_one_action_relation",
                "progress_relation": "system_one_progress_relation",
                "retry_relation": "system_one_retry_relation",
                "goal_drift": "system_one_goal_drift",
                "route_preference": "system_one_route_preference",
            }
            for answer in judgment.answers:
                target = event_labels.get(answer.question_id)
                if target:
                    payload[target] = answer.selected
        self._emit_runtime({"category": "decision", "event_type": "system_one_decision",
                            "subject": subject_ref, "payload": payload})
        return judgment

    def _validate_child_route_bundle(self, route: str, target_statework_id: str,
                                      child: resolve_runtime.RuntimeBundle) -> None:
        """Require a resolver-produced child that changes only one legal flow."""
        pieces = route.split(":", 2)
        if len(pieces) != 3 or pieces[0] != "flow" or pieces[1] != target_statework_id:
            raise ValueError("route must identify the target StateWork flow")
        flow_name = pieces[2]
        if not flow_name or len(flow_name) > 64 or any(char in flow_name for char in "/\\\0"):
            raise ValueError("route flow name is malformed")
        if not isinstance(child, resolve_runtime.RuntimeBundle):
            raise ValueError("legal route must be represented by a resolved runtime bundle")
        resolve_runtime._policy_from_bundle(self.bundle)
        resolve_runtime._policy_from_bundle(child)
        if child.task_id != self.bundle.task_id or child.statework_root != self.bundle.statework_root:
            raise ValueError("child route changed task or StateWork source")

        before, after = self.bundle.policy.canonical(), child.policy.canonical()
        mutable_policy_fields = {"kernel", "capsules", "stateworks"}
        if set(before) != set(after) or any(
                before[key] != after[key] for key in before if key not in mutable_policy_fields):
            raise PermissionError("child route changed policy outside flow-derived content")
        for field_name in (
                "namespace_id", "application_id", "application_instance_id", "agent_id",
                "agent_instance_id", "attempt_id", "interaction_id", "parent_session_id",
                "subject_ref", "goal_contract", "goal_contract_hash", "system_one_policy",
                "policy_revocation_revision", "revocation_mode", "shared_workflow_membership",
                "contracts"):
            if self.bundle.pinned.get(field_name) != child.pinned.get(field_name):
                raise PermissionError(f"child route changed pinned {field_name}")
        if (self.bundle.guard_pack.get("guards") != child.guard_pack.get("guards")
                or self.bundle.guard_pack.get("structural_handlers")
                != child.guard_pack.get("structural_handlers")
                or self.bundle.guard_pack.get("routing_adjustments")
                != child.guard_pack.get("routing_adjustments")
                or self.bundle.guard_pack.get("routing_experiment")
                != child.guard_pack.get("routing_experiment")):
            raise PermissionError("child route changed host guards or routing authority")

        parent_stateworks = list(self.bundle.stateworks)
        child_stateworks = list(child.stateworks)
        if len(parent_stateworks) != len(child_stateworks):
            raise PermissionError("child route changed the required StateWork set")
        parent_target = child_target = None
        for left, right in zip(parent_stateworks, child_stateworks):
            if left.get("id") != right.get("id"):
                raise PermissionError("child route reordered or replaced StateWorks")
            if left.get("id") == target_statework_id:
                parent_target, child_target = left, right
                flow_derived_fields = {"selected_flow", "flow_requirements",
                                       "flow_metadata", "flow_states"}
                for key in set(left) | set(right):
                    if (key not in flow_derived_fields
                            and left.get(key) != right.get(key)):
                        raise PermissionError("child route changed a StateWork contract")
            elif left != right:
                raise PermissionError("child route changed a different StateWork")
        if parent_target is None or child_target is None:
            raise ValueError("route target is not required by the pinned task")
        selected = child_target.get("selected_flow")
        if not isinstance(selected, (list, tuple)) or len(selected) != 2:
            raise ValueError("child bundle has no pinned selected flow")
        relative_path, content_hash = str(selected[0]), str(selected[1])
        flow_path = Path(relative_path)
        if (flow_path.is_absolute() or ".." in flow_path.parts
                or flow_path.parts[:1] != (target_statework_id,)
                or flow_path.parent.name != flow_name):
            raise ValueError("child bundle did not resolve the selected legal flow")
        root = Path(child.statework_root).resolve()
        resolved_flow = (root / flow_path).resolve()
        try:
            resolved_flow.relative_to(root)
        except ValueError as exc:
            raise PermissionError("child flow escaped its StateWork source") from exc
        if not resolved_flow.is_file() or resolve_runtime.sha256(resolved_flow) != content_hash:
            raise resolve_runtime.SnapshotIntegrityError(
                "child flow content no longer matches its resolved snapshot")

    def _record_route_replan_blocker(self, *, target_statework_id: str,
                                     reason: str,
                                     legal_routes: Sequence[str] = ()) -> None:
        self._journal("route_replan_blocked", target_statework_id=target_statework_id,
                      reason=reason, legal_routes=sorted(legal_routes))
        self._emit_runtime({
            "category": "blocker", "event_type": "route_replan_blocked",
            "subject": self.current_segment.subject_ref,
            "payload": {"summary": reason[:256], "result_class": "blocked"}})
    def _rank_legal_routes(
            self, capability: _HostCapability, *,
            legal_children: Mapping[str, resolve_runtime.RuntimeBundle],
            state_revision: int, target_statework_id: str,
            route_preference: Optional[str] = None,
            preference_source: str = "metacognition"
            ) -> Optional[RouteReplanRecommendation]:
        self._require_host(capability)
        if self._evaluate_metacognition().mode != "REPLAN":
            return None
        if self.termination_disposition in TERMINAL_DISPOSITIONS:
            raise PermissionError("a terminated task cannot request a route replan")
        if self._pending_action_requests:
            raise PermissionError("route ranking requires an action boundary with no pending calls")
        if not isinstance(state_revision, int) or state_revision < 0:
            raise ValueError("current StateWork revision must be a non-negative integer")
        if not target_statework_id or len(target_statework_id) > 64:
            raise ValueError("route ranking requires a bounded StateWork ID")
        if not isinstance(legal_children, Mapping) or len(legal_children) > 16:
            raise ValueError("route ranking accepts at most sixteen host-resolved legal flows")
        if preference_source not in {"metacognition", "operator", "learned_profile"}:
            raise ValueError("unsupported deterministic route preference source")
        children = dict(legal_children)
        for route, child in children.items():
            if not isinstance(route, str):
                raise ValueError("legal flow route IDs must be strings")
            self._validate_child_route_bundle(route, target_statework_id, child)
        if not children:
            self._record_route_replan_blocker(
                target_statework_id=target_statework_id,
                reason="no host-resolved legal route is available")
            return None

        selected = route_preference
        source = preference_source
        semantic_decision_ref: Optional[str] = None
        confidence = 1.0
        if selected is not None:
            if selected not in children:
                raise ValueError("preferred route must be one of the host-resolved legal flows")
            if source == "learned_profile":
                adjustments = self.bundle.guard_pack.get("routing_adjustments", ())
                if not any(item.get("route_type") == "flow"
                           and item.get("disposition") == "prefer"
                           and item.get("route") in {selected, selected.split(":", 2)[-1]}
                           for item in adjustments):
                    raise PermissionError("learned route preference is not pinned in this policy")
        elif len(children) == 1:
            selected = next(iter(children))
            source = "metacognition"
        else:
            domains = set(self.bundle.pinned.get("domain_tags") or ())
            task_shape = self.bundle.pinned.get("task_shape")
            operation = self.bundle.pinned.get("operation")
            trigger = self.bundle.pinned.get("trigger")
            current_state = self.statework_states.get(
                (target_statework_id, self.current_segment.subject_ref or ""))
            scores: Dict[str, tuple[int, int]] = {}
            for route, child in children.items():
                pin = next((item for item in child.stateworks
                            if item.get("id") == target_statework_id), None)
                selected_flow = pin.get("selected_flow") if pin else None
                if not selected_flow:
                    scores[route] = (0, 0)
                    continue
                flow_path = Path(child.statework_root) / str(selected_flow[0])
                parts = flow_path.read_text(encoding="utf-8").split("---", 2)
                metadata = yaml.safe_load(parts[1]) if len(parts) == 3 else {}
                metadata = metadata if isinstance(metadata, Mapping) else {}
                hits = 0
                specificity = 0
                for key, value in (("operations", operation), ("triggers", trigger),
                                   ("task_shapes", task_shape)):
                    declared = set(metadata.get(key) or ())
                    if declared:
                        specificity += 1
                        hits += int(value in declared)
                declared_domains = set(metadata.get("domain_tags") or ())
                if declared_domains:
                    specificity += 1
                    hits += len(domains & declared_domains)
                states = metadata.get("states") or {}
                declared_states = set(states.get("from") or ()) if isinstance(states, Mapping) else set()
                if declared_states:
                    specificity += 1
                    hits += int(current_state in declared_states)
                if metadata.get("fallback") is True:
                    hits -= 1
                scores[route] = (hits, specificity)
            top_score = max(scores.values())
            top_routes = sorted(route for route, score in scores.items() if score == top_score)
            if len(top_routes) == 1:
                selected = top_routes[0]
                source = "metacognition"
            else:
                policy = self._system_one_policy()
                if policy.mode != "advisory" or self.system_one_judge is None:
                    self._record_route_replan_blocker(
                        target_statework_id=target_statework_id,
                        reason="legal routes are tied and no calibrated semantic resolver is available",
                        legal_routes=top_routes)
                    return None
                candidate = self.metacognitive_state.make_candidate(
                    "validation", source="host_observed", action_class="route_ranking",
                    host_declared_relation="ambiguous", target_kind="statework",
                    proposed_purpose="resolve equally specific legal StateWork flows",
                    proposed_purpose_source="host_observed")
                judgment = self._system_one_query(
                    questions=("route_preference",), candidate=candidate,
                    subject_ref=self.current_segment.subject_ref,
                    # Semantic inference may resolve only the deterministic
                    # top-score tie. Lower-ranked legal flows are not choice
                    # candidates for the provider.
                    route_candidates=tuple(top_routes),
                    route_state_revision=state_revision,
                    route_statework_id=target_statework_id)
                if judgment is None:
                    self._record_route_replan_blocker(
                        target_statework_id=target_statework_id,
                        reason="semantic route resolution did not produce an answer",
                        legal_routes=top_routes)
                    return None
                decision = self.system_one_decisions.get(judgment.request_id) or {}
                answer = judgment.answer("route_preference")
                calibrated_ids = set(decision.get("calibrated_question_ids") or ())
                confidence = answer.calibrated_confidence if answer else None
                if ("route_preference" not in calibrated_ids or answer is None
                        or confidence is None
                        or confidence < policy.confidence_threshold("route_preference")
                        or answer.selected not in top_routes):
                    self._record_route_replan_blocker(
                        target_statework_id=target_statework_id,
                        reason="semantic route answer is not calibrated for the tied legal candidates",
                        legal_routes=top_routes)
                    return None
                selected = answer.selected
                source = "semantic_advisory"
                semantic_decision_ref = judgment.request_id
        assert selected is not None
        reason = {
            "metacognition": "deterministic_route_specificity",
            "operator": "operator_route_preference",
            "learned_profile": "learned_profile_route_preference",
            "semantic_advisory": "semantic_route_recommendation",
        }[source]
        if semantic_decision_ref:
            decision_id = semantic_decision_ref
            semantic_decision = self.system_one_decisions[decision_id]
            state_digest = semantic_decision["state_digest"]
            request_digest = semantic_decision["request_digest"]
            sequence = int(semantic_decision["sequence"])
        else:
            decision_id = self._record_route_decision(
                capability, route=selected, route_type="flow",
                selection_reason=f"policy_epoch:{reason}",
                event_type="route_replan_recommended")
            state_digest = hashlib.sha256(json.dumps({
                "metacognitive": self.metacognitive_state.snapshot(),
                "state_revision": state_revision,
                "legal_routes": sorted(children),
            }, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
            request_digest = hashlib.sha256(json.dumps({
                "decision_id": decision_id, "route": selected, "source": source,
                "state_digest": state_digest,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            sequence = self.system_one_sequence
        recommendation = RouteReplanRecommendation(
            route=selected, decision_id=decision_id, source=source,
            source_state_digest=state_digest,
            request_digest=request_digest, sequence=sequence,
            confidence=float(confidence), state_revision=state_revision,
            target_statework_id=target_statework_id, legal_children=children,
            semantic_decision_ref=semantic_decision_ref)
        self._journal("route_replan_recommended", recommendation={
            "route": recommendation.route,
            "decision_id": recommendation.decision_id,
            "decision_source": recommendation.source,
            "semantic_decision_ref": recommendation.semantic_decision_ref,
            "source_state_digest": recommendation.source_state_digest,
            "request_digest": recommendation.request_digest,
            "sequence": recommendation.sequence,
            "confidence": recommendation.confidence,
            "state_revision": state_revision,
            "target_statework_id": target_statework_id,
            "legal_routes": sorted(children),
        })
        return recommendation

    def _create_child_policy_epoch(
            self, capability: _HostCapability,
            recommendation: RouteReplanRecommendation, *, selected_route: str,
            current_state_revision: int) -> ChildPolicyEpoch:
        self._require_host(capability)
        if not isinstance(recommendation, RouteReplanRecommendation):
            raise TypeError("child policy epoch requires a host route recommendation")
        if self.termination_disposition in TERMINAL_DISPOSITIONS:
            raise PermissionError("a terminated task cannot enter a child policy epoch")
        if self._pending_action_requests:
            raise PermissionError("child policy epoch requires no pending action calls")
        if recommendation.sequence != self.system_one_sequence:
            raise PermissionError("route recommendation is stale after subsequent System-1 activity")
        if current_state_revision != recommendation.state_revision:
            raise PermissionError("StateWork revision changed after route ranking")
        if selected_route not in recommendation.legal_children:
            raise PermissionError("host selected a route outside the resolved legal candidates")
        if recommendation.source == "semantic_advisory":
            decision = self.system_one_decisions.get(recommendation.decision_id)
            if (not decision or decision.get("policy_hash") != self.bundle.pinned.get("policy_hash")
                    or decision.get("request_digest") != recommendation.request_digest
                    or decision.get("state_digest") != recommendation.source_state_digest
                    or decision.get("sequence") != recommendation.sequence
                    or decision.get("route_state_revision") != current_state_revision
                    or decision.get("route_statework_id") != recommendation.target_statework_id
                    or tuple(decision.get("legal_routes") or ())
                    != tuple(sorted(recommendation.legal_children))):
                raise PermissionError("route recommendation no longer matches its semantic decision")
            answers = decision.get("answers", {})
            answer = answers.get("route_preference") if isinstance(answers, Mapping) else None
            if (not isinstance(answer, Mapping)
                    or answer.get("selected") != recommendation.route):
                raise PermissionError("route recommendation has no matching semantic answer")
        else:
            reasons = {
                "metacognition": "deterministic_route_specificity",
                "operator": "operator_route_preference",
                "learned_profile": "learned_profile_route_preference",
            }
            decision = self.route_decisions.get(recommendation.decision_id)
            if (recommendation.source not in reasons or not decision
                    or decision.get("policy_hash") != self.bundle.pinned.get("policy_hash")
                    or decision.get("route_type") != "flow"
                    or decision.get("route") != recommendation.route
                    or decision.get("selection_reason")
                    != f"policy_epoch:{reasons[recommendation.source]}"):
                raise PermissionError("route recommendation no longer matches its host decision")
        if recommendation.route not in recommendation.legal_children:
            raise PermissionError("route recommendation is outside the legal candidates")
        child_bundle = recommendation.legal_children[selected_route]
        self._validate_child_route_bundle(
            selected_route, recommendation.target_statework_id, child_bundle)
        child_hash = str(child_bundle.pinned.get("policy_hash") or "")
        parent_hash = str(self.bundle.pinned.get("policy_hash") or "")
        if not child_hash or child_hash == parent_hash:
            raise ValueError("child policy epoch must have a distinct policy hash")
        epoch_index = (self.policy_epoch_record.epoch_index + 1
                       if self.policy_epoch_record else 1)
        record = PolicyEpochRecord(
            epoch_index=epoch_index, parent_policy_hash=parent_hash,
            child_policy_hash=child_hash, reason="dynamic_replan",
            route_type="flow", selected_route=selected_route,
            decision_source=recommendation.source,
            decision_ref=recommendation.decision_id,
            semantic_decision_ref=recommendation.semantic_decision_ref,
            state_revision=current_state_revision)
        route_name = selected_route.split(":", 2)[2]
        route_decision_id = self._record_route_decision(
            capability, route=route_name, route_type="flow",
            selection_reason="dynamic_policy_epoch")
        self._journal("child_policy_epoch_created", policy_epoch=record.to_dict(),
                      route_decision_id=route_decision_id,
                      legal_routes=sorted(recommendation.legal_children),
                      recommendation_confidence=recommendation.confidence)
        self._host_event({
            "event_id": f"policy-epoch-{uuid.uuid4()}", "category": "decision",
            "event_type": "policy_epoch_created", "subject": self.bundle.policy.subject_ref,
            "payload": {"summary": "host-created child policy epoch",
                        "route": route_name, "route_type": "flow",
                        "route_decision_id": route_decision_id,
                        "policy_hash": parent_hash,
                        "reason": "dynamic_replan"}})
        return ChildPolicyEpoch(record=record, bundle=child_bundle)

    @staticmethod
    def _validate_system_one_judgment(frame: SystemOneFrame,
                                      judgment: SystemOneJudgment) -> None:
        checks = (
            (judgment.identity == frame.identity, "identity"),
            (judgment.request_id == frame.request_id, "request ID"),
            (judgment.sequence == frame.sequence, "sequence"),
            (judgment.state_digest == frame.state_digest, "state digest"),
            (judgment.question_bundle_digest == frame.question_bundle_digest, "question bundle"),
            (judgment.request_digest == frame.request_digest, "request digest"),
            (judgment.policy_hash == frame.policy_hash, "policy"),
            (judgment.backend_id == frame.backend_id, "backend"),
            (judgment.model_id == frame.model_id, "model"),
            (judgment.model_revision == frame.model_revision, "model revision"),
            (judgment.model_content_hash == frame.model_content_hash, "model content"),
            (judgment.provider_protocol_hash == frame.provider_protocol_hash, "provider protocol"),
            (judgment.calibration_bundle_hash == frame.calibration_bundle_hash, "calibration"),
        )
        for passed, label in checks:
            if not passed:
                raise ValueError(f"System-1 response {label} mismatch")
        if set(answer.question_id for answer in judgment.answers) != set(frame.question_ids):
            raise ValueError("System-1 response question set mismatch")
        for spec in frame.questions:
            answer = judgment.answer(spec.question_id)
            assert answer is not None
            if tuple(item.label for item in answer.raw_probabilities) != tuple(
                    label for label, _ in spec.criteria):
                raise ValueError("System-1 probability labels differ from question options")
        route = judgment.answer("route_preference")
        if route is not None and route.selected not in frame.state.legal_routes:
            raise ValueError("System-1 route preference is outside eligible candidates")

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
            GateDecision.REQUIRE_CHANGED_HYPOTHESIS: 3,
            GateDecision.REQUIRE_GOAL_REANCHOR: 4,
            GateDecision.REQUIRE_RECOVERY: 5,
            GateDecision.BLOCK: 6,
        }
        return left if rank[left] >= rank[right] else right

    def _operator_state_snapshot(self) -> list[dict[str, Any]]:
        """Return bounded StateWork state references without evidence content."""
        subject = self.bundle.policy.subject_ref
        snapshots: list[dict[str, Any]] = []
        for statework in self.bundle.stateworks:
            statework_id = str(statework.get("id") or "")
            contract = statework.get("transitions_contract") or {}
            state = self.statework_states.get((statework_id, subject))
            revision = None
            if self.state_authority is not None and subject:
                try:
                    contract_hash = hashlib.sha256(json.dumps(
                        contract, sort_keys=True, separators=(",", ":"), default=str
                    ).encode("utf-8")).hexdigest()
                    authoritative = self.state_authority.inspect(
                        statework_id=statework_id, subject_ref=subject,
                        contract_hash=contract_hash)
                    state = authoritative.get("state")
                    revision = authoritative.get("revision")
                except Exception as exc:
                    revision = None
                    state = f"unavailable:{type(exc).__name__}"
            snapshots.append({
                "statework_id": statework_id,
                "subject_ref": subject,
                "state": state,
                "revision": revision,
            })
        return snapshots

    def _revocation_current(self) -> bool:
        if self.policy_revocation_authority is None or not self.adopted_profile_hashes:
            return True
        try:
            snapshot = self.policy_revocation_authority.inspect(tuple(self.adopted_profile_hashes))
            revision = int(snapshot.get("revision", 0) or 0)
            revoked = set(snapshot.get("revoked_profile_hashes") or ())
            immediate = bool(snapshot.get("immediate"))
            if not revoked.intersection(self.adopted_profile_hashes):
                self.policy_revocation_revision = max(self.policy_revocation_revision, revision)
                return True
            if self.revocation_mode == "immediate" or immediate:
                return False
            # Deferred: the pinned policy remains intact until the next task.
            self.policy_revocation_revision = max(self.policy_revocation_revision, revision)
            return True
        except Exception as exc:
            self._emit_runtime({"category": "blocker", "event_type": "policy_revocation_unavailable",
                                "payload": {"summary": type(exc).__name__,
                                            "result_class": "reinspection required"}})
            return False

    def _operator_trace(self) -> dict[str, Any]:
        """Correlate bounded authority references; never expose prompt or raw evidence."""
        advice = self.bundle.guard_pack.get("advice_exchange") or {}
        advice_context = advice.get("context") if isinstance(advice, Mapping) else {}
        advice_sources = advice.get("adjustments") if isinstance(advice, Mapping) else []
        return {
            "trace_version": "1.0.0",
            "task": {
                "task_id": self.bundle.task_id,
                "session_id": self.session_id,
                "attempt_id": self.attempt_id,
                "agent_instance_id": self.agent_instance_id,
            },
            "policy": {
                "policy_hash": self.bundle.pinned.get("policy_hash"),
                "static_policy_hash": (advice_context or {}).get("static_policy_hash"),
                "eligible_choice_hash": (advice_context or {}).get("eligible_choice_hash"),
                "context_identity": (advice_context or {}).get("context_identity"),
            },
            "statework": self._operator_state_snapshot(),
            "advice": {
                "advice_id": (advice_context or {}).get("advice_version"),
                "source_profile_hash": (advice_context or {}).get("source_profile_hash"),
                "expires_at": (advice_context or {}).get("expires_at"),
                "adjustment_count": len(advice_sources or []),
            },
            "route_decisions": [dict(value) for value in self.route_decisions.values()],
            "route_outcomes": [dict(value) for value in self.route_outcomes.values()],
        }

    def _membership_current(self) -> bool:
        membership = self.shared_workflow_membership
        if not membership:
            return True
        if self.membership_authority is None:
            return False
        return self.membership_authority.validate(
            membership, session=self, operation="transition")

    def _authoritative_state_ready(self) -> bool:
        if self.runtime_mode != "combined_enforcing":
            return True
        if self.state_authority is None or not self.bundle.stateworks:
            return False
        try:
            for statework in self.bundle.stateworks:
                statework_id = str(statework.get("id") or "")
                contract = statework.get("transitions_contract") or {}
                subject = self.bundle.policy.subject_ref
                contract_hash = hashlib.sha256(json.dumps(
                    contract, sort_keys=True, separators=(",", ":"), default=str
                ).encode("utf-8")).hexdigest()
                snapshot = self.state_authority.inspect(
                    statework_id=statework_id, subject_ref=subject,
                    contract_hash=contract_hash)
                key = (statework_id, subject)
                current = str(snapshot.get("state") or contract.get("initial_state") or "")
                self.statework_states[key] = current
                self.statework_entry_times[key] = str(snapshot.get("updated_at") or "")
                object.__setattr__(
                    self, "authority_revision",
                    max(self.authority_revision, int(snapshot.get("revision", 0) or 0)))
            return True
        except Exception as exc:
            self._emit_runtime({"category": "blocker", "event_type": "state_authority_unavailable",
                                "subject": self.bundle.policy.subject_ref,
                                "payload": {"summary": str(exc)[:512], "result_class": "blocked"}})
            return False

    def before_action(self, action: Dict[str, Any] | AgentActionRequest) -> GateDecision:
        """Compatibility gate for adapters that do not need result correlation."""
        return self.prepare_action(action).decision

    def prepare_action(self, action: Dict[str, Any] | AgentActionRequest) -> ActionPreparation:
        """Authorize an action and return its unique request ID for its eventual result."""
        request = self._request(action)
        action_request_id = f"action-request-{uuid.uuid4()}"
        if request.subject_ref == self.bundle.policy.subject_ref and not self._authoritative_state_ready():
            return ActionPreparation(action_request_id, GateDecision.BLOCK,
                                     self._descriptor(request))
        if not self._revocation_current() or not self._membership_current():
            return ActionPreparation(action_request_id, GateDecision.BLOCK,
                                     self._descriptor(request))
        descriptor = self._descriptor(request)
        decision = self._decision(request)
        strategy = self._action_strategy()
        candidate: Optional[ActivityFact] = None
        for hook in self.tool_hooks:
            hook_decision = hook({"tool_binding_id": request.binding_id,
                                  "arguments": dict(request.arguments),
                                  "subject_ref": request.subject_ref})
            if hook_decision is not None:
                if not isinstance(hook_decision, GateDecision):
                    raise ValueError("tool hooks must return GateDecision or None")
                decision = self._more_restrictive(decision, hook_decision)

        # System-1 sees this action only after every hard host check. Semantic
        # action relevance informs attention, while the host descriptor alone
        # determines hard budgets and gate outcomes.
        if decision == GateDecision.ALLOW:
            target_ref = next((request.arguments.get(key) for key in ("target", "path", "resource")
                               if isinstance(request.arguments.get(key), str)), None)
            candidate = self.metacognitive_state.make_candidate(
                "action", source="host_observed",
                action_class=descriptor.action_class or descriptor.tool_type,
                host_declared_relation=descriptor.goal_relation,
                target_kind=descriptor.target_kind,
                target_scope=descriptor.target_scope,
                target_ref=target_ref,
                proposed_purpose=descriptor.goal_intent,
                proposed_purpose_source="host_observed",
                affected_criterion_ids=descriptor.affected_criterion_ids)
            goal = self.bundle.pinned.get("goal_contract") or {}
            if goal and descriptor.goal_relation == "ambiguous":
                self._system_one_query(
                    questions=("action_relation", "goal_drift"), candidate=candidate,
                    subject_ref=request.subject_ref, action_request_id=action_request_id,
                    descriptor=descriptor)
            host_relation = descriptor.goal_relation
            candidate = replace(candidate, host_declared_relation=host_relation)
            verification = (goal.get("verification_policy") or {})
            if host_relation == "required_verification":
                violation = self.metacognitive_state.verification_limit_exceeded(
                    candidate, relation=host_relation,
                    max_actions=int(verification.get(
                        "max_verification_actions_without_artifact_change", 2) or 2),
                    max_repeats=int(verification.get(
                        "max_repeated_equivalent_verifications", 1) or 1))
                if violation:
                    decision = GateDecision.BLOCK
                    self._emit_runtime({
                        "category": "blocker", "event_type": "verification_action_limited",
                        "subject": request.subject_ref,
                        "payload": {"summary": violation, "result_class": "blocked"}})
            if (goal.get("work_mode") == "delivery" and self.convergence is not None
                    and host_relation not in {"advances_primary_goal", "removes_concrete_blocker"}
                    and self.convergence.needs_goal_reanchor(
                        cause_id="primary-objective",
                        support_limit=int(verification.get(
                            "support_actions_before_goal_reanchor", 5) or 5))):
                decision = GateDecision.REQUIRE_GOAL_REANCHOR

        if decision == GateDecision.ALLOW:
            self._pending_action_requests[action_request_id] = {
                "descriptor": descriptor.__dict__.copy(),
                "subject_ref": request.subject_ref,
                "sequence": self.system_one_sequence,
                "relation": descriptor.goal_relation,
                "candidate": candidate,
            }
            attempt_ref = str(request.arguments.get("attempt_ref") or "")
            if attempt_ref and attempt_ref in self._pending_retry_candidates:
                self._pending_action_requests[action_request_id]["retry_candidate"] = (
                    self._pending_retry_candidates.pop(attempt_ref))
            self._journal("action_request_prepared", action_request_id=action_request_id,
                          subject_ref=request.subject_ref, descriptor=descriptor.__dict__,
                          relation=self._pending_action_requests[action_request_id]["relation"])
        self._emit_runtime({"category": "action", "event_type": "action_requested",
                   "subject": request.subject_ref,
                   "payload": {"summary": decision.value,
                               "tool_type": descriptor.tool_type,
                               "route": request.binding_id,
                               "route_type": "tool_policy",
                               "action_request_id": action_request_id,
                               "advice_priority": strategy["priorities"].get(request.binding_id),
                               "policy_hash": strategy["policy_hash"]}})
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
        if decision != GateDecision.ALLOW:
            self._pending_action_requests.pop(action_request_id, None)
        return ActionPreparation(action_request_id, decision, descriptor)

    def before_retry(self, request: Mapping[str, Any] | AgentActionRequest) -> GateDecision:
        """Dispatch retry-specific structural handlers at the host boundary."""
        action = self._request(request)
        if self.termination_disposition in TERMINAL_DISPOSITIONS:
            # A terminated task must not be revived by a retry request.
            return GateDecision.BLOCK
        attempt_ref = str(action.arguments.get("attempt_ref") or "")
        attempt = self.attempt_descriptors.get(attempt_ref)
        if attempt is None:
            return GateDecision.REQUIRE_REINSPECTION
        semantic = self.attempt_semantics.get(attempt_ref)
        candidate = self.metacognitive_state.make_candidate(
            "retry", source="host_observed",
            action_class=(semantic.planned_action_class if semantic else "retry"),
            host_declared_relation="ambiguous",
            target_kind=(semantic.planned_target_kind if semantic else "attempt"),
            proposed_purpose=(semantic.hypothesis_summary if semantic else None),
            proposed_purpose_source=(semantic.source if semantic else "derived"),
            affected_criterion_ids=(semantic.affected_criterion_ids if semantic else ()))
        self._semantic_retry_same.discard(attempt_ref)
        if semantic is not None and self._system_one_policy().mode != "disabled":
            state = self.convergence.state(attempt.cause_id) if self.convergence else None
            deterministic_same = bool(state and
                state.action_fingerprint == attempt.action_plan_digest
                and state.hypothesis_fingerprint == attempt.premise_digest
                and state.premise_digest == attempt.premise_digest)
            judgment = None
            decision = None
            if not deterministic_same:
                judgment = self._system_one_query(
                    questions=("retry_relation",), candidate=candidate,
                    subject_ref=action.subject_ref)
                decision = (self.system_one_decisions.get(judgment.request_id)
                            if judgment else None)
            retry_relation = fuse_retry_relation(
                deterministic_same=deterministic_same, judgment=judgment,
                policy=self._system_one_policy(),
                calibrated=tuple((decision or {}).get("calibrated_question_ids") or ()))
            if retry_relation == "equivalent_retry" and not deterministic_same:
                # Inference can collapse novelty to the already-recorded branch;
                # it cannot create a new branch or reset any host budget.
                self._semantic_retry_same.add(attempt_ref)
        try:
            for enforcement in self.bundle.guard_pack.get("structural_handlers", {}).values():
                if enforcement.get("mode") != "structural":
                    continue
                handler = handler_for_enforcement(enforcement)
                if handler and "before_retry" in getattr(handler, "hooks", ()):
                    result = handler.before_retry(self, action, enforcement)
                    if result == "block":
                        if (self.convergence is not None
                                and self.convergence.state(attempt.cause_id).disposition == "BLOCKED"):
                            state = self.convergence.state(attempt.cause_id)
                            self.request_termination(
                                self._host_capability, disposition="BLOCKED",
                                reason="Autonomous recovery limit reached without primary-goal progress.",
                                cause_id=attempt.cause_id,
                                evidence_refs=([state.last_failure_evidence_ref]
                                               if state.last_failure_evidence_ref else ()))
                        return GateDecision.BLOCK
                    if result == "require_reinspection":
                        return GateDecision.REQUIRE_REINSPECTION
                    if result == "require_changed_hypothesis":
                        return GateDecision.REQUIRE_CHANGED_HYPOTHESIS
                    if result == "require_goal_reanchor":
                        return GateDecision.REQUIRE_GOAL_REANCHOR
            retry_relation = ("equivalent_retry" if attempt_ref in self._semantic_retry_same
                              else "different_fingerprint")
            self._pending_retry_candidates[attempt_ref] = replace(
                candidate, result_class=retry_relation)
            return GateDecision.ALLOW
        finally:
            self._semantic_retry_same.discard(attempt_ref)

    def _retry_comparison_fingerprints(
            self, descriptor: AttemptDescriptor) -> tuple[str, str, Optional[str]]:
        if descriptor.attempt_ref not in self._semantic_retry_same or self.convergence is None:
            return (descriptor.action_plan_digest, descriptor.premise_digest,
                    descriptor.premise_digest)
        prior = self.convergence.state(descriptor.cause_id)
        return (prior.action_fingerprint or descriptor.action_plan_digest,
                prior.hypothesis_fingerprint or descriptor.premise_digest,
                prior.premise_digest or descriptor.premise_digest)

    def request_termination(self, capability: _HostCapability, *,
                           disposition: str, reason: str, cause_id: str,
                           evidence_refs: Iterable[str] = (),
                           last_verified_state: Optional[str] = None,
                           unresolved_blockers: Iterable[str] = ()) -> TerminationPermit:
        """Terminate as blocked/failed/handoff without claiming success."""
        self._require_host(capability)
        if disposition not in {"BLOCKED", "FAILED", "HANDOFF"}:
            raise ValueError("termination disposition must be BLOCKED, FAILED or HANDOFF")
        if self.termination_disposition == "SUCCEEDED":
            raise PermissionError("a completed task cannot be re-terminated")
        resolved_refs: list[str] = []
        for ref in evidence_refs:
            record = self.ledger.get(str(ref))
            if record is None or record.invalidated:
                continue
            resolved_refs.append(str(ref))
        claim_digest = hashlib.sha256(json.dumps({
            "task_id": self.bundle.task_id, "policy_hash": self.bundle.pinned["policy_hash"],
            "subject_ref": self.bundle.policy.subject_ref, "disposition": disposition,
            "cause_id": cause_id, "reason": reason, "evidence_refs": sorted(resolved_refs),
            "last_verified_state": last_verified_state,
            "unresolved_blockers": sorted(str(item) for item in unresolved_blockers),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        permit = TerminationPermit(
            task_id=self.bundle.task_id, policy_hash=self.bundle.pinned["policy_hash"],
            subject_ref=self.bundle.policy.subject_ref, disposition=disposition,
            cause_id=cause_id, reason=reason, claim_digest=claim_digest,
            evidence_refs=tuple(sorted(resolved_refs)),
            issued_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            session_nonce=self.session_nonce, authority_revision=self.authority_revision,
            last_verified_state=last_verified_state,
            unresolved_blockers=tuple(sorted(str(item) for item in unresolved_blockers)))
        self.termination_disposition = disposition
        self.termination_permit = permit
        if self.convergence is not None:
            self.convergence.terminate(disposition=disposition, reason=reason)
            self._record_convergence(f"termination:{disposition}")
        self._journal("task_terminated", disposition=disposition, reason=reason,
                      cause_id=cause_id, claim_digest=claim_digest,
                      evidence_refs=list(permit.evidence_refs),
                      last_verified_state=last_verified_state,
                      unresolved_blockers=list(permit.unresolved_blockers))
        # behavior-event.schema.json forbids extra top-level fields, so causal
        # lineage travels in the bounded payload rather than a new column.
        event = {"category": "blocker", "event_type": "task_terminated",
                 "subject": self.bundle.policy.subject_ref,
                 "payload": {"summary": reason, "result_class": disposition,
                             # cause lineage travels in the bounded `reason`
                             # field; the payload schema is closed.
                             "reason": cause_id}}
        # A blocker may legitimately have no evidence refs; the schema requires
        # a non-empty list when the field is present, so omit it rather than
        # inventing a reference.
        if permit.evidence_refs:
            event["evidence_refs"] = list(permit.evidence_refs)
        self._emit_runtime(event)
        return permit

    def finish_task_terminated(self, output: Optional[Dict[str, Any]] = None,
                               *, permit: Optional[TerminationPermit] = None) -> Dict[str, Any]:
        """Finalize a blocked/failed/handoff task using its TerminationPermit."""
        permit = permit or self.termination_permit
        if permit is None or not isinstance(permit, TerminationPermit):
            raise PermissionError("termination requires a host-issued TerminationPermit")
        if permit.task_id != self.bundle.task_id or permit.policy_hash != self.bundle.pinned["policy_hash"]:
            raise PermissionError("termination permit is not bound to this task/policy")
        if permit.session_nonce != self.session_nonce or permit is not self.termination_permit:
            raise PermissionError("termination permit is not bound to this runtime session")
        if permit.disposition != self.termination_disposition:
            raise PermissionError("termination permit disposition disagrees with the runtime")
        if permit.authority_revision != self.authority_revision:
            raise PermissionError("termination permit predates a later authority revision")
        return self._render_output({
            **(dict(output) if output else {}),
            "status": permit.disposition.lower(),
            "reason": permit.reason,
            "cause_id": permit.cause_id,
            "completed": False,
            "last_verified_state": permit.last_verified_state,
            "unresolved_blockers": list(permit.unresolved_blockers),
        }, complete=True)

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

    def _record_convergence(self, reason: str) -> None:
        """Persist convergence state so resume cannot erase the budget."""
        if self.convergence is None:
            return
        self._journal("convergence_recorded", reason=reason,
                      convergence=self.convergence.export())

    def _system_one_decision_for_action(self, capability: _HostCapability,
                                        action_request_id: str) -> Optional[str]:
        self._require_host(capability)
        if (not isinstance(action_request_id, str)
                or not action_request_id.startswith("action-request-")
                or len(action_request_id) > 256):
            raise ValueError("action_request_id must be a bounded host-generated identifier")
        matches = [decision for decision in self.system_one_decisions.values()
                   if decision.get("action_request_id") == action_request_id
                   and decision.get("judgment")]
        if not matches:
            return None
        matches.sort(key=lambda item: int(item.get("sequence", 0)))
        return str(matches[-1]["decision_id"])

    def _record_system_one_outcome(self, capability: _HostCapability, *,
                                   decision_id: str, label: str, value: str,
                                   evidence_ref: str, authority: str,
                                   criterion_id: Optional[str] = None) -> str:
        """Append T2 ground truth only from later host-attested evidence."""
        self._require_host(capability)
        decision = self.system_one_decisions.get(decision_id)
        if decision is None or not decision.get("judgment"):
            raise ValueError("System-1 outcome requires a recorded prediction")
        outcome_key = f"{decision_id}:{label}"
        if outcome_key in self.system_one_outcomes:
            raise ValueError("System-1 outcome already recorded for this label")
        label_values = {
            "action_relation": {"advances_primary_goal", "removes_concrete_blocker",
                                "required_verification", "diagnostic_support", "unrelated"},
            "progress_relation": {"goal_progress", "blocker_removed", "diagnostic_progress",
                                  "verification_progress", "recovery_progress", "no_progress"},
            "retry_relation": {"materially_new", "same_premise_new_action",
                               "equivalent_retry", "unrelated"},
            "goal_drift": {"yes", "no"},
        }
        prediction = next((item for item in decision["judgment"].get("answers", ())
                           if item.get("question_id") == label), None)
        if prediction is None:
            raise ValueError("outcome label was not included in the prediction")
        if label == "route_preference":
            label_values[label] = {str(item.get("label")) for item in
                                   prediction.get("raw_probabilities", ())
                                   if isinstance(item, Mapping)}
        if label not in label_values or value not in label_values[label]:
            raise ValueError("unsupported System-1 ground-truth label")
        if authority not in {"host_evidence", "validator", "csw_fact", "operator"}:
            raise ValueError("unsupported System-1 outcome authority")
        record = self.ledger.get(evidence_ref)
        if record is None or record.invalidated:
            raise ValueError("System-1 outcome requires current host-attested evidence")
        expected_subject = decision.get("subject_ref") or self.bundle.policy.subject_ref
        if expected_subject and record.subject_ref != expected_subject:
            raise ValueError("System-1 outcome evidence subject does not match its decision")
        issuer = str(record.issuer or "")
        issuer_prefixes = {
            "host_evidence": ("host:", "runtime:"),
            "validator": ("validator:",),
            "csw_fact": ("statework:", "csw:"),
            "operator": ("operator:",),
        }
        if not issuer.startswith(issuer_prefixes[authority]):
            raise PermissionError("evidence issuer does not match declared outcome authority")
        observed = datetime.fromisoformat(str(record.observed_at).replace("Z", "+00:00"))
        decided = datetime.fromisoformat(str(decision["timestamp"]).replace("Z", "+00:00"))
        if observed <= decided:
            raise ValueError("System-1 ground truth must be observed after its prediction")
        if label == "progress_relation" and value in {"goal_progress", "blocker_removed"}:
            criteria = {str(item.get("criterion_id")) for item in
                        (self.bundle.pinned.get("goal_contract") or {}).get(
                            "acceptance_criteria", ())}
            if not criterion_id or criterion_id not in criteria:
                raise ValueError("goal progress outcome must match a pinned acceptance criterion")
        elif criterion_id is not None:
            criteria = {str(item.get("criterion_id")) for item in
                        (self.bundle.pinned.get("goal_contract") or {}).get(
                            "acceptance_criteria", ())}
            if criterion_id not in criteria:
                raise ValueError("outcome references an unknown acceptance criterion")
        self.system_one_outcomes.add(outcome_key)
        outcome_event_id = f"system-one-outcome-{uuid.uuid4()}"
        self._journal("system_one_outcome_recorded", decision_id=decision_id,
                      label=label, value=value, evidence_ref=evidence_ref,
                      authority=authority, outcome_event_id=outcome_event_id)
        label_key = {
            "action_relation": "system_one_outcome_action_relation",
            "progress_relation": "system_one_outcome_progress_relation",
            "retry_relation": "system_one_outcome_retry_relation",
            "goal_drift": "system_one_outcome_goal_drift",
            "route_preference": "system_one_outcome_route_preference",
        }[label]
        self._emit_runtime({
            "event_id": outcome_event_id,
            "category": "validation", "event_type": "system_one_outcome",
            "subject": record.subject_ref, "evidence_refs": [evidence_ref],
            "payload": {"summary": "independent System-1 evaluation outcome",
                        "system_one_outcome_for": decision_id,
                        label_key: value,
                        "system_one_outcome_authority": authority,
                        "system_one_outcome_evidence_ref": evidence_ref,
                        "system_one_independent_outcome": True,
                        "criterion_id": criterion_id or ""}})
        return outcome_event_id

    def _shared_branch_exhausted(self, descriptor: AttemptDescriptor,
                                 recovery_limit: int) -> bool:
        membership = self.shared_workflow_membership or {}
        if self.shared_branch_ledger is None or not membership.get("workflow_id"):
            return False
        count = self.shared_branch_ledger.recovery_count(
            namespace_id=str(membership.get("namespace_id") or ""),
            application_instance_id=str(membership.get("application_instance_id") or ""),
            workflow_id=str(membership.get("workflow_id") or ""),
            cause_id=descriptor.cause_id, premise_digest=descriptor.premise_digest)
        if count < recovery_limit:
            return False
        if self.convergence is not None:
            state = self.convergence.state(descriptor.cause_id)
            state.disposition = "BLOCKED"
            state.terminated_reason = "shared_premise_recovery_limit"
            self._record_convergence("shared_premise_recovery_limit")
        return True

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
                            claim_digest: Optional[str] = None,
                            schema_id: Optional[str] = None,
                            validator_id: Optional[str] = None) -> str:
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
            claim_digest=claim_digest, schema=schema_id, validator=validator_id)
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
                      schema=attestation.schema,
                      validator=attestation.validator,
                      invalidated=attestation.invalidated)
        return evidence_id

    def _record_typed_authority(self, capability: _HostCapability, evidence_type: str,
                                issuer: str, validator: str, schema_id: str,
                                content: Mapping[str, Any], subject_ref: Optional[str]) -> str:
        self._require_host(capability)
        if not isinstance(content, Mapping) or not content:
            raise TypeError(f"{evidence_type} requires a non-empty typed mapping")
        required = {
            "rollback_plan": {"plan_id", "intended_state", "steps"},
            "rollback": {"plan_id", "result", "restored_state"},
        }.get(evidence_type, set())
        if required and not required.issubset(content):
            raise ValueError(f"{evidence_type} is missing typed fields: {sorted(required - set(content))}")
        return self._record_attestation(
            evidence_type=evidence_type, subject_ref=subject_ref, issuer=issuer,
            event={"category": "validation", "event_type": f"{evidence_type}_attested",
                   "subject": subject_ref,
                   "payload": {"summary": evidence_type, "result_class": "validated"}},
            invocation_id=None, content=dict(content), schema_id=schema_id,
            validator_id=validator)

    def _record_authorization(self, capability: _HostCapability,
                              authorization: Mapping[str, Any], *,
                              subject_ref: Optional[str] = None) -> str:
        self._require_host(capability)
        required = {"authorization_id", "principal", "scope", "decision", "issued_at"}
        if not isinstance(authorization, Mapping) or not required.issubset(authorization):
            raise ValueError("authority evidence requires an explicit host authorization record")
        if authorization.get("decision") not in {"allow", "authorized"}:
            raise PermissionError("authority record is not an authorization")
        return self._record_typed_authority(
            capability, "authority", "host:authorization", "host-authorization-v1",
            "authority-record-v1", authorization, subject_ref)

    def _record_repository_truth(self, capability: _HostCapability,
                                 packet: Mapping[str, Any],
                                 observation_context: Mapping[str, Any], *,
                                 subject_ref: Optional[str] = None) -> str:
        self._require_host(capability)
        if not isinstance(observation_context, Mapping) or not observation_context:
            raise ValueError("repository truth requires trusted freshness context")
        control = _statework_transition_engines(
            Path(self.bundle.statework_root or resolve_runtime.COW_ROOT))
        store = control.PacketStore(
            packet_registry_path=Path(self.bundle.statework_root or resolve_runtime.COW_ROOT) /
            "schemas" / "packet-registry.yaml")
        if packet.get("packet_type") != "repository_truth_packet":
            raise ValueError("repository truth adapter accepts only repository truth packets")
        if not store.validate_current(dict(packet), observation_context=observation_context):
            raise ValueError("repository truth packet failed trusted freshness validation")
        subject_ref = subject_ref or packet.get("subject_ref")
        return self._record_attestation(
            evidence_type="repository_truth", subject_ref=subject_ref,
            issuer="validator:repository-truth-v1",
            event={"category": "validation", "event_type": "repository_truth_validated",
                   "subject": subject_ref,
                   "payload": {"summary": "trusted repository truth", "result_class": "validated"}},
            invocation_id=None, content=dict(packet),
            schema_id="repository-truth-packet.schema.json",
            validator_id="repository-truth-v1")

    def _record_repository_evidence(self, capability: _HostCapability,
                                    packet: Mapping[str, Any],
                                    validation: Mapping[str, Any], *,
                                    subject_ref: Optional[str] = None) -> str:
        self._require_host(capability)
        if not isinstance(validation, Mapping) or validation.get("valid") is not True:
            raise ValueError("repository evidence requires a passing post-mutation validation result")
        if not isinstance(packet, Mapping) or packet.get("packet_type") != "repository_evidence_packet":
            raise ValueError("repository evidence adapter accepts only repository evidence packets")
        return self._record_attestation(
            evidence_type="repository_evidence", subject_ref=subject_ref or packet.get("subject_ref"),
            issuer="validator:repository-evidence-v1",
            event={"category": "validation", "event_type": "repository_evidence_validated",
                   "subject": subject_ref or packet.get("subject_ref"),
                   "payload": {"summary": "post-mutation repository validation", "result_class": "validated"}},
            invocation_id=None, content={"packet": dict(packet), "validation": dict(validation)},
            schema_id="repository-evidence-packet.schema.json",
            validator_id="repository-evidence-v1")

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
        candidate = experiment.get("candidate_adjustment") or {}
        intervention_assigned = experiment.get("routing_cohort") == "treatment"
        intervention_executed = bool(
            intervention_assigned
            and candidate.get("route_type") == route_type
            and str(candidate.get("route") or "") == route
        )
        effective_policy_identity = (
            experiment.get("cohort_policy_identity")
            if experiment.get("routing_cohort") in {"control", "treatment", "holdout"}
            else self.bundle.pinned.get("policy_hash")
        )
        self.route_decisions[decision_id] = {
            "decision_id": decision_id, "route": route, "route_type": route_type,
            "policy_hash": self.bundle.pinned.get("policy_hash"),
            "selection_reason": selection_reason,
            "candidate_profile_hash": experiment.get("candidate_profile_hash"),
            "candidate_adjustment_applied": experiment.get("candidate_adjustment_applied", False),
            "intervention_assigned": intervention_assigned,
            "intervention_executed": intervention_executed,
            "effective_policy_identity": effective_policy_identity,
            "comparison_context_hash": experiment.get("comparison_context_hash"),
            "experiment_plan_hash": experiment.get("plan_hash"),
            "routing_cohort": experiment.get("routing_cohort"),
            "policy_identity": experiment.get("cohort_policy_identity"),
            "evaluator_version": experiment.get("target_evaluator_version"),
            "evaluator_hash": experiment.get("target_evaluator_hash"),
        }
        self._journal("route_decision_recorded", decision_id=decision_id,
                      decision=dict(self.route_decisions[decision_id]))
        route_payload = {"summary": route, "route": route, "route_type": route_type,
                         "route_decision_id": decision_id,
                         "policy_hash": self.bundle.pinned.get("policy_hash"),
                         "reason": selection_reason}
        route_payload.update({key: value for key, value in {
            "experiment_plan_hash": experiment.get("plan_hash"),
            "routing_cohort": experiment.get("routing_cohort"),
            "policy_identity": effective_policy_identity,
            "intervention_assigned": intervention_assigned,
            "intervention_executed": intervention_executed,
            "effective_policy_hash": self.bundle.pinned.get("policy_hash"),
            "parent_decision_ref": self.parent_decision_ref,
            "shared_evidence_refs": list(self.shared_evidence_refs),
            "delegation_topology": self.delegation_topology,
            "premise_digest": self.premise_digest,
            "failure_attribution": self.failure_attribution,
        }.items() if value is not None})
        self._host_event({
            "event_id": decision_id, "category": "decision", "event_type": event_type,
            "subject": self.bundle.policy.subject_ref,
            "payload": route_payload})
        return decision_id

    def _evaluate_route_outcome(self, capability: _HostCapability, *,
                                route_decision_id: str, evaluator_id: str,
                                evidence_refs: List[str]) -> str:
        self._require_host(capability)
        decision = self.route_decisions.get(route_decision_id)
        if decision is None:
            raise ValueError("route outcome requires a current host route decision")
        experiment = self.bundle.guard_pack.get("routing_experiment") or {}
        expected_evaluator = experiment.get("target_evaluator") or "route-success-v1"
        if evaluator_id != expected_evaluator:
            raise ValueError(f"unknown route evaluator {evaluator_id!r}")
        resolved = self.ledger.resolve(list(evidence_refs), subject_ref=self.bundle.policy.subject_ref)
        # Tri-state: absence of success evidence is NOT failure evidence.
        outcome = "NOT_APPLICABLE"
        for ref in resolved:
            record = self.ledger.get(ref["ref"])
            if record is None or record.kind != "validation_result":
                continue
            invocation = self.host_invocations.get(record.invocation_id or "", {})
            actual = invocation.get("actual_result") or {}
            status = str(actual.get("status") or actual.get("result") or "").lower()
            if status in {"pass", "passed", "success", "ok"}:
                outcome = "PASS"
                break
            if status in {"fail", "failed", "failure", "error"}:
                outcome = "FAIL"
                break
        event_id = f"evt-{uuid.uuid4()}"
        outcome_payload = {
            "summary": decision["route"], "route": decision["route"],
            "route_type": decision["route_type"], "route_decision_id": route_decision_id,
            "evaluator_id": evaluator_id, "outcome_source": f"evaluator:{evaluator_id}",
            "outcome": outcome, "policy_hash": decision["policy_hash"],
            "candidate_adjustment_applied": decision.get("candidate_adjustment_applied", False),
            "intervention_assigned": decision.get("intervention_assigned", False),
            "intervention_executed": decision.get("intervention_executed", False),
            "effective_policy_hash": decision.get("effective_policy_identity"),
            "reason": "registered_host_evaluator",
        }
        outcome_payload.update({key: value for key, value in {
            "evaluator_version": decision.get("evaluator_version"),
            "evaluator_hash": decision.get("evaluator_hash"),
            "candidate_profile_hash": decision.get("candidate_profile_hash"),
            "comparison_context_hash": decision.get("comparison_context_hash"),
            "experiment_plan_hash": decision.get("experiment_plan_hash"),
            "routing_cohort": decision.get("routing_cohort"),
            "policy_identity": decision.get("policy_identity"),
        }.items() if value is not None})
        # An outcome needs an origin and an intended subject: "task-123 got a
        # retry failure" is ambiguous when several agents share that task.
        self.route_outcomes[event_id] = {
            "event_id": event_id,
            "route_decision_id": route_decision_id,
            "evaluator_id": evaluator_id,
            "outcome": outcome,
            "evidence_refs": list(evidence_refs),
            "origin": {
                "agent_instance_id": self.agent_instance_id,
                "session_id": self.session_id,
                "attempt_id": self.attempt_id,
                "delegation_id": self.delegation_id,
                "interaction_id": self.interaction_id,
                "parent_decision_ref": self.parent_decision_ref,
                "premise_digest": self.premise_digest,
                "failure_attribution": self.failure_attribution,
            },
            "target": {
                "scope": (self.shared_workflow_membership or {}).get("workflow_id")
                          and "shared_workflow" or "agent_private",
                "workflow_id": (self.shared_workflow_membership or {}).get("workflow_id"),
                "interaction_id": self.interaction_id,
                "agent_instance_id": self.agent_instance_id,
            },
        }
        self._journal("route_outcome_recorded", event_id=event_id,
                      outcome=dict(self.route_outcomes[event_id]))
        self._host_event({
            "event_id": event_id, "category": "decision", "event_type": "route_outcome",
            "subject": self.bundle.policy.subject_ref,
            "evidence_refs": list(evidence_refs),
            "payload": outcome_payload})
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
        if observation_type in AUTHORITATIVE_EVIDENCE_KINDS:
            raise ValueError(
                f"{observation_type} is registered transition authority; use its typed host adapter")
        control = _statework_transition_engines(
            Path(self.bundle.statework_root or resolve_runtime.COW_ROOT))
        evidence_registry = control.load_evidence_kind_registry(
            Path(self.bundle.statework_root or resolve_runtime.COW_ROOT) /
            "schemas" / "evidence-kinds.yaml")
        if observation_type in set((evidence_registry.get("kinds") or {})):
            raise ValueError(
                f"{observation_type} is a registered evidence kind; use the StateWork evidence adapter")
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

    def _record_statework_evidence(self, capability: _HostCapability,
                                   evidence_type: str, *, subject_ref: str,
                                   content: Mapping[str, Any]) -> str:
        """Attest an explicitly registered host observation for a StateWork edge."""
        self._require_host(capability)
        if not isinstance(subject_ref, str) or not subject_ref.strip():
            raise ValueError("StateWork evidence requires a subject reference")
        if not isinstance(content, Mapping) or not content:
            raise ValueError("StateWork evidence requires non-empty host-captured content")
        root = Path(self.bundle.statework_root or resolve_runtime.COW_ROOT)
        control = _statework_transition_engines(root)
        registry = control.load_evidence_kind_registry(root / "schemas" / "evidence-kinds.yaml")
        spec = (registry.get("kinds") or {}).get(evidence_type)
        if not spec or spec.get("mintable_by_host") is not True:
            raise PermissionError(
                f"StateWork evidence kind {evidence_type!r} requires a specialized validator")
        issuers = list(spec.get("allowed_issuers") or [])
        if len(issuers) != 1:
            raise ValueError("host-minted StateWork evidence requires one registered issuer")
        return self._record_attestation(
            evidence_type=evidence_type, subject_ref=subject_ref,
            issuer=issuers[0],
            event={"category": "validation", "event_type": f"statework_{evidence_type}_attested",
                   "subject": subject_ref,
                   "payload": {"summary": f"registered StateWork evidence: {evidence_type}",
                               "result_class": "validated"}},
            invocation_id=None, content=dict(content), schema_id=spec["schema"],
            validator_id=spec["validator"])

    def _host_blocker_evidence(self, evidence_ref: str) -> EvidenceAttestation:
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("blocker changes require an evidence reference")
        record = self.ledger.get(evidence_ref)
        if record is None or record.invalidated:
            raise ValueError("blocker changes require current host-attested evidence")
        expected_subject = self.bundle.policy.subject_ref
        if expected_subject and record.subject_ref != expected_subject:
            raise ValueError("blocker evidence subject does not match the active task")
        if not str(record.issuer or "").startswith(("host:", "validator:", "csw:")):
            raise ValueError("blocker evidence requires a host, validator, or CSW authority")
        return record

    def _record_blocker(self, capability: _HostCapability, *, blocker_id: str,
                        summary: str, evidence_ref: str,
                        criterion_ids: Iterable[str] = ()) -> None:
        self._require_host(capability)
        if not isinstance(blocker_id, str) or not blocker_id.strip() or len(blocker_id) > 128:
            raise ValueError("blocker ID must be bounded non-empty text")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 256:
            raise ValueError("blocker summary must be bounded non-empty text")
        record = self._host_blocker_evidence(evidence_ref)
        criteria = tuple(dict.fromkeys(str(item) for item in criterion_ids))[:8]
        declared = {str(item.get("criterion_id")) for item in
                    (self.bundle.pinned.get("goal_contract") or {}).get(
                        "acceptance_criteria", ())}
        if declared and any(item not in declared for item in criteria):
            raise ValueError("blocker references an unknown acceptance criterion")
        self.metacognitive_state.record_blocker(
            blocker_id, summary, criteria, evidence_ref=evidence_ref)
        self._journal("metacognitive_blocker_recorded",
                      blocker_id=blocker_id[:128], evidence_ref=evidence_ref,
                      snapshot=self.metacognitive_state.snapshot())
        self._emit_runtime({
            "category": "blocker", "event_type": "host_blocker_recorded",
            "subject": record.subject_ref, "evidence_refs": [evidence_ref],
            "payload": {"summary": summary[:256], "result_class": "active",
                        "blocker_id": blocker_id[:128],
                        "criterion_ids": list(criteria)}})

    def _clear_blocker(self, capability: _HostCapability, *, blocker_id: str,
                       evidence_ref: str) -> None:
        self._require_host(capability)
        if not isinstance(blocker_id, str) or not blocker_id.strip() or len(blocker_id) > 128:
            raise ValueError("blocker ID must be bounded non-empty text")
        record = self._host_blocker_evidence(evidence_ref)
        existing = next((item for item in self.metacognitive_state.blockers
                         if item.blocker_id == blocker_id), None)
        if existing is None:
            raise ValueError("cannot clear an unknown metacognitive blocker")
        self.metacognitive_state.clear_blocker(blocker_id)
        self._journal("metacognitive_blocker_cleared", blocker_id=blocker_id[:128],
                      evidence_ref=evidence_ref,
                      snapshot=self.metacognitive_state.snapshot())
        self._emit_runtime({
            "category": "blocker", "event_type": "host_blocker_cleared",
            "subject": record.subject_ref, "evidence_refs": [evidence_ref],
            "payload": {"summary": f"resolved: {existing.summary}"[:256],
                        "result_class": "resolved", "blocker_id": blocker_id[:128]}})

    def _record_tool_result(self, capability: _HostCapability, tool_type: str,
                            result_class: str, subject_ref: Optional[str] = None,
                            *, invocation_id: Optional[str] = None,
                            result_digest: Optional[str] = None,
                            action_request_id: Optional[str] = None,
                            actual_result: Optional[Mapping[str, Any]] = None) -> str:
        """Record a tool result from the host execution channel."""
        self._require_host(capability)
        invocation_id = invocation_id or f"invocation-{uuid.uuid4()}"
        if not isinstance(invocation_id, str) or not invocation_id.strip() or len(invocation_id) > 256:
            raise ValueError("invocation_id must be a bounded host-generated identifier")
        result_data = dict(actual_result or {})
        raw_evidence_refs = result_data.get("evidence_refs") or ()
        if (not isinstance(raw_evidence_refs, (list, tuple))
                or len(raw_evidence_refs) > 8
                or any(not isinstance(ref, str) or not ref for ref in raw_evidence_refs)):
            raise ValueError("tool-result evidence_refs must contain at most eight host references")
        supporting_evidence: list[EvidenceRecord] = []
        for evidence_ref in dict.fromkeys(raw_evidence_refs):
            record = self.ledger.get(evidence_ref)
            if (record is None or record.invalidated
                    or record.subject_ref != subject_ref):
                raise ValueError("tool-result evidence reference is missing, stale, or cross-subject")
            supporting_evidence.append(record)
        supported_progress_kinds = {
            "rendered_observation", "validator_result", "repository_evidence",
            "state_transition", "completion_boundary",
        }
        supporting_kinds = tuple(dict.fromkeys(
            record.kind for record in supporting_evidence
            if record.kind in supported_progress_kinds))
        supporting_refs = tuple(record.evidence_id for record in supporting_evidence
                                if record.kind in supported_progress_kinds)
        pending = (self._pending_action_requests.get(action_request_id or "")
                   if action_request_id else None)
        if action_request_id and pending is None:
            raise ValueError("tool result references an unknown action_request_id")
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
            "action_request_id": action_request_id,
            "actual_result": dict(actual_result or {}),
        }
        self._journal("tool_invocation_completed", invocation_id=invocation_id,
                      tool_type=tool_type, result_class=result_class,
                      subject_ref=subject_ref, evidence_id=evidence_id,
                      action_request_id=action_request_id,
                      result_digest=result_digest or "",
                      actual_result_digest=hashlib.sha256(
                          json.dumps(dict(actual_result or {}), sort_keys=True,
                                     separators=(",", ":"), default=str).encode("utf-8")
                      ).hexdigest())
        descriptor = None
        if pending is not None:
            descriptor = ActionDescriptor(**dict(pending["descriptor"]))
        changed = result_data.get("artifact_changed")
        if not isinstance(changed, bool):
            changed = None
        evidence_kinds = tuple(dict.fromkeys(("tool_result",) + supporting_kinds))
        if pending is not None and isinstance(pending.get("candidate"), ActivityFact):
            candidate = replace(
                pending["candidate"], result_class=result_class[:64],
                artifact_changed=changed, evidence_kinds=evidence_kinds)
            candidate = self.metacognitive_state.commit_candidate(
                candidate,
                verification_relation=("required_verification"
                                       if pending.get("relation") == "required_verification"
                                       else None))
            retry_candidate = pending.get("retry_candidate")
            if isinstance(retry_candidate, ActivityFact):
                self.metacognitive_state.commit_candidate(retry_candidate)
        else:
            candidate = self.metacognitive_state.record_activity(
                "tool_result", source="host_observed",
                action_class=(descriptor.action_class if descriptor else tool_type[:64]),
                host_declared_relation=(pending.get("relation", "ambiguous")
                                        if pending else "ambiguous"),
                target_kind=(descriptor.target_kind if descriptor else None),
                target_scope=(descriptor.target_scope if descriptor else None),
                result_class=result_class[:64], artifact_changed=changed,
                affected_criterion_ids=(descriptor.affected_criterion_ids if descriptor else ()),
                evidence_kinds=evidence_kinds)
        self._journal("system_one_projection_changed",
                      snapshot=self.metacognitive_state.snapshot())
        if (self.convergence is not None
                and (self.bundle.pinned.get("goal_contract") or {}).get("work_mode") == "delivery"):
            host_relation = str((pending or {}).get("relation") or "ambiguous")
            if host_relation not in {"advances_primary_goal", "removes_concrete_blocker",
                                     "required_verification"}:
                # Unknown and ambiguous operations count conservatively as
                # support. No tool-name list decides task relevance.
                state = self.convergence.record_support_action(cause_id="primary-objective")
                self._record_convergence("support_action")
                self._record_support_action_event(
                    evidence_id=evidence_id, subject_ref=subject_ref,
                    support_count=state.support_actions_since_goal_progress)
        if self._system_one_policy().mode != "disabled":
            self._system_one_query(
                questions=("progress_relation",), candidate=candidate,
                subject_ref=subject_ref,
                action_request_id=action_request_id,
                action_invocation_id=invocation_id)
            decision = next((item for item in reversed(tuple(self.system_one_decisions.values()))
                             if item.get("action_invocation_id") == invocation_id), None)
            judgment_data = dict((decision or {}).get("judgment") or {})
            try:
                judgment = (SystemOneJudgment.from_mapping(judgment_data)
                            if judgment_data else None)
            except (TypeError, ValueError):
                judgment = None
            criterion_id = next((item for item in (descriptor.affected_criterion_ids
                                                    if descriptor else ())
                                 if any(row.criterion_id == item and row.status != "satisfied"
                                        for row in self.metacognitive_state.criteria)), None)
            criterion = next((row for row in self.metacognitive_state.criteria
                              if row.criterion_id == criterion_id), None)
            host_progress_class = str(result_data.get("progress_class") or "")
            fused_progress = fuse_progress_relation(
                host_progress_class=host_progress_class,
                criterion_id=criterion_id,
                judgment=judgment, policy=self._system_one_policy(),
                calibrated=tuple((decision or {}).get("calibrated_question_ids") or ()))
            self._emit_runtime({
                "category": "tool_result", "event_type": "progress_relation_fused",
                "subject": subject_ref,
                "evidence_refs": [evidence_id, *supporting_refs],
                "payload": {"summary": "System-1 supplied a non-authoritative progress meaning hint",
                            "result_class": fused_progress.relation,
                            "progress_relation": fused_progress.relation,
                            "criterion_id": criterion_id or "",
                            "model_influenced": fused_progress.model_influenced,
                            "source_decision_id": fused_progress.decision_id or ""}})
        if action_request_id:
            self._pending_action_requests.pop(action_request_id, None)
        self._complete_action(capability)
        return evidence_id

    def _record_support_action_event(self, *, evidence_id: str,
                                     subject_ref: Optional[str], support_count: int) -> None:
        self._emit_runtime({
            "category": "tool_result", "event_type": "support_action_recorded",
            "subject": subject_ref, "evidence_refs": [evidence_id],
            "payload": {"summary": "host-classified action tracked as support",
                        "result_class": "support_progress",
                        "progress_class": "DIAGNOSTIC_PROGRESS",
                        "support_actions_since_goal_progress": support_count}})

    def _complete_action(self, capability: _HostCapability) -> None:
        self._require_host(capability)
        if self.lifecycle is not None:
            prior = set(self.active_lifecycle)
            self.lifecycle.on_action_complete()
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
            self._emit_lifecycle_activations(prior)

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
        evidence_id = self._record_attestation(
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
            claim_digest=claim_digest, schema_id=f"validator-result:{evidence_type}",
            validator_id=validator_id)
        if self.convergence is not None:
            self.convergence.record_progress(
                cause_id="primary-objective",
                progress_digest=hashlib.sha256(json.dumps({
                    "validator_id": validator_id, "evidence_id": evidence_id,
                    "result_class": evidence_type,
                }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
                progress_class="VERIFICATION_PROGRESS", evidence_ref=evidence_id)
            self._record_convergence("verification_progress")
        return evidence_id

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
        evidence_registry = control.load_evidence_kind_registry()
        engine = control.TransitionEngine(
            contract, evidence_registry=evidence_registry, require_registered=True)
        state_key = (statework_id, subject_ref)
        contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        if self.state_authority is not None:
            authoritative = self.state_authority.inspect(
                statework_id=statework_id, subject_ref=subject_ref, contract_hash=contract_hash)
            current = str(authoritative.get("state") or contract["initial_state"])
            authoritative_revision = int(authoritative.get("revision", 0) or 0)
        else:
            authoritative = None
            authoritative_revision = 0
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
        decision = engine.transition(subject_ref, origin, output_state, trigger=trigger,
                                     evidence_refs=verified_evidence)
        transition_facts = decision.progress_facts()
        self._emit_runtime({"category": "state_transition", "event_type": "transition_attempted",
                   "subject": subject_ref, "input_state": origin, "output_state": output_state,
                   "payload": {"summary": decision.reason,
                               "result_class": "allowed" if decision.allowed else "blocked"}})
        if not decision.allowed:
            return GateDecision.BLOCK
        if self.state_authority is not None:
            try:
                committed = self.state_authority.commit(
                    statework_id=statework_id, subject_ref=subject_ref,
                    contract_hash=contract_hash, expected_revision=authoritative_revision,
                    output_state=output_state, evidence_refs=[item["ref"] for item in verified_evidence],
                    trigger=trigger or "", evidence_records=verified_evidence)
            except Exception as exc:
                self._emit_runtime({"category": "blocker", "event_type": "state_authority_conflict",
                           "subject": subject_ref, "input_state": origin, "output_state": output_state,
                           "payload": {"summary": str(exc)[:512], "result_class": "re-inspection required"}})
                return GateDecision.REQUIRE_REINSPECTION
            authoritative_revision = int(committed.get("revision", authoritative_revision + 1) or authoritative_revision + 1)
            transition_facts = dict(committed.get("transition") or transition_facts)
        manifest = statework
        prior_revision = sum(
            1 for item in self.state_segments
            if item.statework_id == statework_id and item.subject_ref == subject_ref)
        transition_facts.update({
            "trigger": trigger,
            "revision": (authoritative_revision if self.state_authority is not None
                         else prior_revision + 1),
            "domain": str(manifest.get("domain") or "") or None,
            "purpose": str(manifest.get("purpose") or "") or None,
        })
        self.statework_states[state_key] = output_state
        self.metacognitive_state.record_csw_fact(
            statework_id=statework_id, input_state=origin, output_state=output_state,
            progress_class=str(transition_facts.get("progress_class") or "LATERAL"),
            workflow_metadata=transition_facts)
        self._journal("system_one_projection_changed",
                      snapshot=self.metacognitive_state.snapshot())
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
        csw_progress_class = str(transition_facts.get("progress_class") or "LATERAL")
        if csw_progress_class in {"RECOVERY", "INVALIDATION", "REGRESSION"}:
            progress_class = "RECOVERY_PROGRESS"
        elif csw_progress_class == "TERMINAL":
            progress_class = "VERIFICATION_PROGRESS"
        elif csw_progress_class == "FORWARD_PROGRESS":
            progress_class = "DIAGNOSTIC_PROGRESS"
        else:
            progress_class = "DIAGNOSTIC_PROGRESS"
        if self.convergence is not None:
            progress_digest = hashlib.sha256(json.dumps({
                "statework_id": statework_id, "subject_ref": subject_ref,
                "input_state": origin, "output_state": output_state,
                "edge_id": transition_facts.get("edge_id"),
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            if csw_progress_class in {"RECOVERY", "INVALIDATION", "REGRESSION"}:
                self.convergence.record_recovery_epoch(cause_id="primary-objective")
            elif csw_progress_class in {"FORWARD_PROGRESS", "TERMINAL"}:
                self.convergence.record_progress(
                    cause_id="primary-objective", progress_digest=progress_digest,
                    progress_class=progress_class,
                    evidence_ref=(verified_evidence[-1]["ref"] if verified_evidence else None))
            else:
                self.convergence.record_support_action(cause_id="primary-objective")
        self._journal("state_transition_allowed", statework_id=statework_id,
                      subject_ref=subject_ref, input_state=origin,
                      output_state=output_state, evidence_refs=[item["ref"] for item in verified_evidence],
                      recorded_at=entry_time, segment_id=state_segment.segment_id,
                      transition=transition_facts,
                      progress_class=progress_class,
                      csw_progress_class=csw_progress_class)
        self._record_convergence("state_transition_progress")
        self._emit_runtime({
            "category": "state_transition", "event_type": "state_transition_progress",
            "subject": subject_ref,
            "input_state": origin, "output_state": output_state,
            "evidence_refs": [item["ref"] for item in verified_evidence],
            "payload": {"summary": "CSW transition progress is tracked separately from task-goal progress",
                        "result_class": progress_class.lower(),
                        "progress_class": progress_class,
                        "csw_progress_class": csw_progress_class}})
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
        if not self._authoritative_state_ready():
            raise PermissionError("shared StateWork authority is unavailable for handoff")
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
        packet_meta = self._packet_store.registry.get(packet.get("packet_type"), {})
        freshness_validator = packet_meta.get("freshness_validator")
        if freshness_validator and observation_context is None:
            raise ValueError("freshness validation context is mandatory for this handoff packet")
        if not freshness_validator and packet_meta.get("freshness") != "immutable":
            raise ValueError("packet registry must explicitly declare freshness semantics")
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

    def _begin_attempt(self, capability: _HostCapability, *, attempt_ref: str,
                       cause_id: str, premise_digest: str,
                       action_plan: Mapping[str, Any],
                       parent_attempt_ref: Optional[str] = None,
                       semantic_descriptor: Optional[Mapping[str, Any]] = None) -> AttemptDescriptor:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("attempt tracking requires the host convergence controller")
        if not isinstance(action_plan, Mapping):
            raise TypeError("action_plan must be host-accepted structured data")
        encoded = json.dumps(dict(action_plan), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("action_plan exceeds the bounded attempt contract")
        descriptor = AttemptDescriptor(
            attempt_ref=attempt_ref, cause_id=cause_id,
            premise_digest=premise_digest,
            action_plan_digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            parent_attempt_ref=parent_attempt_ref)
        semantic = None
        if semantic_descriptor is not None:
            if not isinstance(semantic_descriptor, Mapping):
                raise TypeError("semantic_descriptor must be a bounded host-accepted mapping")
            allowed = {"cause_class", "hypothesis_summary", "mechanism", "evidence_target",
                       "planned_action_class", "planned_target_kind", "failed_result_class",
                       "affected_criterion_ids", "source"}
            if set(semantic_descriptor) - allowed:
                raise ValueError("semantic_descriptor contains unknown fields")
            semantic = AttemptSemanticDescriptor(
                cause_class=str(semantic_descriptor.get("cause_class") or cause_id)[:64],
                hypothesis_summary=str(semantic_descriptor.get("hypothesis_summary") or "")[:256],
                mechanism=str(semantic_descriptor.get("mechanism") or "")[:256],
                evidence_target=str(semantic_descriptor.get("evidence_target") or "")[:128],
                planned_action_class=str(semantic_descriptor.get("planned_action_class") or "")[:64],
                planned_target_kind=str(semantic_descriptor.get("planned_target_kind") or "")[:64],
                failed_result_class=str(semantic_descriptor.get("failed_result_class") or "pending")[:64],
                premise_digest=premise_digest,
                action_plan_digest=descriptor.action_plan_digest,
                affected_criterion_ids=tuple(semantic_descriptor.get("affected_criterion_ids") or ()),
                source=str(semantic_descriptor.get("source") or "model_proposed"))
        prior = self.attempt_descriptors.get(attempt_ref)
        if prior is not None and prior != descriptor:
            raise ValueError("attempt_ref is already bound to a different plan")
        self.attempt_descriptors[attempt_ref] = descriptor
        if semantic is not None:
            old_semantic = self.attempt_semantics.get(attempt_ref)
            if old_semantic is not None and old_semantic != semantic:
                raise ValueError("attempt_ref is already bound to a different semantic descriptor")
            self.attempt_semantics[attempt_ref] = semantic
        self._journal("attempt_descriptor_recorded", descriptor=descriptor.to_dict(),
                      semantic_descriptor=semantic.to_dict() if semantic else None)
        return descriptor

    def _record_attempt_failure(self, capability: _HostCapability, *,
                                attempt_ref: str, evidence_ref: str) -> None:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("attempt tracking requires the host convergence controller")
        descriptor = self.attempt_descriptors.get(attempt_ref)
        record = self.ledger.get(evidence_ref)
        if descriptor is None or record is None or record.invalidated:
            raise ValueError("attempt failure requires a registered attempt and current evidence")
        if attempt_ref in self.attempt_failures:
            return
        membership = self.shared_workflow_membership or {}
        if self.shared_branch_ledger is not None and membership.get("workflow_id"):
            self.shared_branch_ledger.record_failure(
                namespace_id=str(membership["namespace_id"]),
                application_instance_id=str(membership["application_instance_id"]),
                workflow_id=str(membership["workflow_id"]),
                cause_id=descriptor.cause_id,
                premise_digest=descriptor.premise_digest,
                action_plan_fingerprint=descriptor.action_plan_digest,
                evidence_ref=evidence_ref, attempt_ref=attempt_ref)
        self.convergence.record_failure(
            cause_id=descriptor.cause_id,
            action_fingerprint=descriptor.action_plan_digest,
            hypothesis_fingerprint=descriptor.premise_digest,
            evidence_ref=evidence_ref, premise_digest=descriptor.premise_digest)
        semantic = self.attempt_semantics.get(attempt_ref)
        if semantic is not None:
            failed = AttemptSemanticDescriptor(
                cause_class=semantic.cause_class,
                hypothesis_summary=semantic.hypothesis_summary,
                mechanism=semantic.mechanism,
                evidence_target=semantic.evidence_target,
                planned_action_class=semantic.planned_action_class,
                planned_target_kind=semantic.planned_target_kind,
                failed_result_class=str(self.host_invocations.get(
                    self.ledger.get(evidence_ref).invocation_id or "", {}).get(
                        "result_class") or semantic.failed_result_class),
                premise_digest=semantic.premise_digest,
                action_plan_digest=semantic.action_plan_digest,
                affected_criterion_ids=semantic.affected_criterion_ids,
                source=semantic.source)
            self.metacognitive_state.record_failed_approach(failed)
            self._journal("system_one_projection_changed",
                          snapshot=self.metacognitive_state.snapshot())
        self.attempt_failures.add(attempt_ref)
        self._journal("attempt_failure_recorded", attempt_ref=attempt_ref,
                      cause_id=descriptor.cause_id, evidence_ref=evidence_ref,
                      convergence=self.convergence.export())
        self._record_convergence("attempt_failure")
        self._emit_runtime({
            "category": "retry", "event_type": "attempt_failure_recorded",
            "subject": record.subject_ref,
            "evidence_refs": [evidence_ref],
            "payload": {"summary": "host observed an unsuccessful attempt",
                        "result_class": "failure",
                        "premise_digest": descriptor.premise_digest,
                        "action_fingerprint": descriptor.action_plan_digest[:64]}})
        return None

    def _record_reinspection(self, capability: _HostCapability, *,
                             cause_id: str, revision: int) -> None:
        self._require_host(capability)
        if self.convergence is None or revision < 1:
            raise ValueError("reinspection requires convergence state and a positive revision")
        self.convergence.record_reinspection(cause_id=cause_id, revision=revision)
        self._record_convergence("reinspection")
        self._emit_runtime({
            "category": "observation", "event_type": "reinspection_recorded",
            "subject": self.bundle.policy.subject_ref,
            "payload": {"summary": "host recorded a fresh inspection revision",
                        "result_class": "reinspection",
                        "revision": revision}})

    def _record_goal_progress(self, capability: _HostCapability, *,
                              cause_id: str, evidence_ref: str,
                              criterion_id: Optional[str] = None,
                              progress_class: str = "GOAL_PROGRESS") -> None:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("goal progress requires the host convergence controller")
        if progress_class not in {"GOAL_PROGRESS", "BLOCKER_REMOVED"}:
            raise ValueError("goal progress must be GOAL_PROGRESS or BLOCKER_REMOVED")
        record = self.ledger.get(evidence_ref)
        if record is None or record.invalidated:
            raise ValueError("goal progress requires current host-attested evidence")
        expected_subject = self.bundle.policy.subject_ref
        if expected_subject and record.subject_ref != expected_subject:
            raise ValueError("goal progress evidence subject does not match the active task")
        supported_kinds = {
            "validator_result", "repository_evidence", "repository_truth",
            "state_transition", "completion_boundary", "rendered_observation",
        }
        if record.kind not in supported_kinds:
            raise ValueError("goal progress requires criterion-bound host evidence of an accepted kind")
        authority = (
            "validator" if str(record.issuer or "").startswith("validator:") else
            "operator" if str(record.issuer or "").startswith("operator:") else
            "csw_authoritative" if str(record.issuer or "").startswith("csw:") else
            "host_observed" if str(record.issuer or "").startswith("host:") else None)
        if authority is None:
            raise ValueError("goal progress evidence issuer is not a recognized host authority")
        criteria = {
            str(item.get("criterion_id")) for item in
            (self.bundle.pinned.get("goal_contract") or {}).get("acceptance_criteria", ())
        }
        if criteria and criterion_id is None:
            raise ValueError("goal progress must match a pinned acceptance criterion")
        if criterion_id is not None and criterion_id not in criteria:
            raise ValueError("goal progress references an unknown acceptance criterion")
        if criterion_id is None:
            raise ValueError("goal progress requires a criterion evidence binding")
        criterion = next((item for item in
                          (self.bundle.pinned.get("goal_contract") or {}).get(
                              "acceptance_criteria", ())
                          if str(item.get("criterion_id")) == criterion_id), None)
        if criterion is None:
            raise ValueError("goal progress criterion is not present in the pinned task contract")
        binding = CriterionEvidenceBinding(
            criterion_id=criterion_id, evidence_ref=evidence_ref,
            evidence_kind=record.kind, authority=authority,
            artifact_revision=record.content_digest)
        digest = hashlib.sha256(json.dumps({
            "evidence_ref": evidence_ref, "criterion_id": criterion_id,
            "content_digest": record.content_digest,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        progress_causes = {cause_id}
        if (self.bundle.pinned.get("goal_contract") or {}).get("work_mode") == "delivery":
            progress_causes.add("primary-objective")
        for progress_cause in progress_causes:
            self.convergence.record_progress(
                cause_id=progress_cause, progress_digest=digest,
                progress_class=progress_class, evidence_ref=evidence_ref)
        self._record_convergence("goal_progress")
        self.metacognitive_state.record_goal_progress(
            binding, claim=str(criterion.get("claim") or ""),
            kind=str(criterion.get("kind") or "acceptance"))
        self._journal("system_one_projection_changed",
                      snapshot=self.metacognitive_state.snapshot())
        self._emit_runtime({
            "category": "decision", "event_type": "goal_progress_recorded",
            "subject": record.subject_ref, "evidence_refs": [evidence_ref],
            "payload": {"summary": "host attested progress on the primary objective",
                        "result_class": "goal_progress",
                        "progress_class": progress_class,
                        "criterion_id": criterion_id or ""}})

    def _record_recovery_epoch(self, capability: _HostCapability, *,
                               cause_id: str, reason: str) -> None:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("recovery tracking requires the host convergence controller")
        state = self.convergence.record_recovery_epoch(cause_id=cause_id)
        self.metacognitive_state.record_recovery_epoch()
        self._journal("system_one_projection_changed",
                      snapshot=self.metacognitive_state.snapshot())
        self._record_convergence("recovery_epoch")
        self._emit_runtime({
            "category": "retry", "event_type": "recovery_epoch_recorded",
            "subject": self.bundle.policy.subject_ref,
            "payload": {"summary": reason[:512],
                        "result_class": "recovery",
                        "progress_class": "RECOVERY_PROGRESS",
                        "recovery_epochs_since_goal_progress":
                            state.recovery_epochs_since_goal_progress}})
        if state.recovery_epochs_since_goal_progress >= int(
                ((self.bundle.pinned.get("goal_contract") or {}).get(
                    "verification_policy") or {}).get(
                        "max_recovery_epochs_without_goal_progress", 3)):
            self.request_termination(
                capability, disposition="BLOCKED",
                reason=(reason or "Recovery limit reached without primary-goal progress.")[:512],
                cause_id=cause_id,
                evidence_refs=([state.last_failure_evidence_ref]
                               if state.last_failure_evidence_ref else ()))

    def _reject_investigative_path(self, capability: _HostCapability, *,
                                   attempt_ref: str, evidence_ref: str) -> None:
        self._require_host(capability)
        if self.convergence is None:
            raise PermissionError("path rejection requires the host convergence controller")
        descriptor = self.attempt_descriptors.get(attempt_ref)
        record = self.ledger.get(evidence_ref)
        if descriptor is None or record is None or record.invalidated:
            raise ValueError("path rejection requires a registered attempt and current evidence")
        self.convergence.reject_path(
            cause_id=descriptor.cause_id,
            action_fingerprint=descriptor.action_plan_digest,
            hypothesis_fingerprint=descriptor.premise_digest,
            evidence_ref=evidence_ref, premise_digest=descriptor.premise_digest,
            observed_at=record.observed_at)
        self._record_convergence("investigative_path_rejected")
        self._emit_runtime({
            "category": "correction", "event_type": "investigative_path_rejected",
            "subject": record.subject_ref, "evidence_refs": [evidence_ref],
            "payload": {"summary": "host rejected an investigative branch",
                        "result_class": "rejected",
                        "premise_digest": descriptor.premise_digest,
                        "action_fingerprint": descriptor.action_plan_digest[:64]}})

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

    def _criterion_evidence_kinds(self, kind: str) -> set[str]:
        defaults = {
            "static_visual": {"rendered_observation"},
            "visual_static": {"rendered_observation"},
            "responsive_layout": {"rendered_viewport_matrix"},
            "interaction": {"interaction_validation"},
            "terminal_lifecycle": {"pty_validation"},
            "legibility": {"rendered_observation"},
            "code_behavior": {"tool_result"},
            "test_suite": {"tool_result"},
        }
        canonical_kind = "static_visual" if kind == "visual_static" else kind
        required = set(defaults.get(canonical_kind, ()))
        for statework in self.bundle.stateworks:
            metadata = statework.get("flow_metadata") or {}
            completion = metadata.get("completion") or {}
            claim_classes = completion.get("claim_classes") or {}
            item = claim_classes.get(canonical_kind)
            if isinstance(item, Mapping):
                required.update(str(value) for value in item.get("evidence_kinds") or ())
        return required

    def _validate_criterion_proofs(self, criterion_ids: Iterable[str],
                                   criterion_evidence_refs: Iterable[tuple[str, Iterable[str]]],
                                   subject_ref: Optional[str]) -> None:
        goal = self.bundle.pinned.get("goal_contract") or {}
        criteria = {str(item.get("criterion_id")): item
                    for item in goal.get("acceptance_criteria", ())}
        proofs = {str(key): tuple(refs) for key, refs in criterion_evidence_refs}
        if set(proofs) - set(criteria):
            raise ValueError("criterion evidence references an unknown criterion")
        for criterion_id in criterion_ids:
            criterion = criteria.get(str(criterion_id))
            refs = proofs.get(str(criterion_id), ())
            if criterion is None or not refs:
                raise ValueError(f"criterion {criterion_id!r} lacks host-attested evidence")
            expected_kinds = self._criterion_evidence_kinds(str(criterion.get("kind") or ""))
            self.ledger.resolve(
                list(refs), subject_ref=subject_ref,
                allowed_kinds=expected_kinds or None)

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
        goal_contract = self.bundle.pinned.get("goal_contract") or {}
        expected_goal_hash = str(self.bundle.pinned.get("goal_contract_hash") or "")
        required_criterion_ids = {
            str(item.get("criterion_id"))
            for item in goal_contract.get("acceptance_criteria", ())
            if item.get("required", True)}
        known_criterion_ids = {
            str(item.get("criterion_id"))
            for item in goal_contract.get("acceptance_criteria", ())}
        claim_criterion_ids = set(claim.criterion_ids)
        if not expected_goal_hash or not required_criterion_ids:
            self._emit_runtime({
                "category": "blocker", "event_type": "completion_blocked_by_goal_contract",
                "subject": subject,
                **({"evidence_refs": evidence_refs} if evidence_refs else {}),
                "payload": {"summary": "successful completion requires a pinned task goal contract",
                            "result_class": "task goal contract required"}})
            return GateDecision.REQUIRE_REINSPECTION
        if (claim.goal_contract_hash != expected_goal_hash
                or not required_criterion_ids.issubset(claim_criterion_ids)
                or not claim_criterion_ids.issubset(known_criterion_ids)):
            self._emit_runtime({
                "category": "blocker", "event_type": "completion_blocked_by_claim",
                "subject": subject, "evidence_refs": evidence_refs,
                "payload": {"summary": "completion claim does not cover the pinned task criteria",
                            "result_class": "task contract criteria required"}})
            return GateDecision.REQUIRE_REINSPECTION
        try:
            self._validate_criterion_proofs(
                claim_criterion_ids, claim.criterion_evidence_refs, subject)
        except ValueError as exc:
            self._emit_runtime({
                "category": "blocker", "event_type": "completion_blocked_by_criterion_evidence",
                "subject": subject,
                "payload": {"summary": str(exc)[:512],
                            "result_class": "criterion evidence required"}})
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
                        or claim.expected_validator != expected_validator
                        or claim.goal_contract_hash != expected_goal_hash):
                    raise ValueError("completion claim identity does not match this task")
                if claim.invocation_id not in {
                        record.invocation_id for record in boundary_records}:
                    raise ValueError("completion claim invocation is not the attested invocation")
            for record in boundary_records:
                expected = completion_claim_digest(
                    self.bundle.task_id, subject, boundary_type, boundary_target,
                    expected_validator, record.invocation_id or "",
                    expected_goal_hash, tuple(claim.criterion_ids),
                    tuple(claim.criterion_evidence_refs))
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
                               "evidence_reference": claim_digest or "",
                               "criterion_ids": sorted(claim_criterion_ids),
                               "completion_criterion_coverage": (
                                   len(required_criterion_ids & claim_criterion_ids)
                                   / max(1, len(required_criterion_ids)))}})
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
            expected_validator=expected_validator,
            goal_contract_hash=expected_goal_hash,
            criterion_ids=tuple(sorted(claim_criterion_ids)),
            criterion_evidence_refs=tuple(claim.criterion_evidence_refs)))
        self._journal("completion_permit_issued", claim_digest=claim_digest,
                      evidence_refs=list(evidence_refs), subject_ref=subject,
                      issued_at=self._completion_permit.issued_at,
                      authority_revision=self.authority_revision,
                      boundary_type=boundary_type,
                      boundary_target=boundary_target,
                      expected_validator=expected_validator,
                      goal_contract_hash=expected_goal_hash,
                      criterion_ids=sorted(claim_criterion_ids),
                      criterion_evidence_refs=[
                          [key, list(refs)]
                          for key, refs in claim.criterion_evidence_refs])
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
            self.lifecycle.on_response_complete()
            self.active_lifecycle = set(self.lifecycle.active)
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
        if not self._authoritative_state_ready():
            raise PermissionError("shared StateWork authority is unavailable for completion")
        if not self._revocation_current():
            raise PermissionError("pinned policy has been immediately revoked")
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
                    record.invocation_id or "", permit.goal_contract_hash,
                    tuple(permit.criterion_ids), tuple(permit.criterion_evidence_refs))
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
        goal_contract = self.bundle.pinned.get("goal_contract") or {}
        required_criterion_ids = {
            str(item.get("criterion_id"))
            for item in goal_contract.get("acceptance_criteria", ())
            if item.get("required", True)}
        if (permit.goal_contract_hash != self.bundle.pinned.get("goal_contract_hash", "")
                or not required_criterion_ids.issubset(set(permit.criterion_ids))):
            raise PermissionError("completion permit does not prove the pinned task contract")
        try:
            self._validate_criterion_proofs(
                permit.criterion_ids, permit.criterion_evidence_refs, permit.subject_ref)
        except ValueError as exc:
            raise PermissionError(f"completion permit criterion evidence is invalid: {exc}") from exc
        for statework in self.bundle.stateworks:
            statework_id = statework.get("id")
            contract = statework.get("transitions_contract") or {}
            completion_states = set(contract.get("completion_states") or ())
            flow_completion = ((statework.get("flow_metadata") or {}).get(
                "completion") or {}).get("claim_classes") or {}
            goal_criteria = [
                item for item in (goal_contract.get("acceptance_criteria") or ())
                if item.get("required", True)]
            applicable = [flow_completion.get(
                "static_visual" if str(item.get("kind") or "") == "visual_static"
                else str(item.get("kind") or ""))
                          for item in goal_criteria]
            applicable = [item for item in applicable if isinstance(item, Mapping)]
            if applicable:
                required_states = [str(item.get("minimum_state") or "")
                                   for item in applicable if item.get("minimum_state")]
                normal_rank = {str(state): index for index, state in enumerate(
                    contract.get("normal_path") or ())}
                minimum_rank = max((normal_rank.get(state, 0) for state in required_states),
                                   default=0)
                required_state = max(required_states,
                                     key=lambda state: normal_rank.get(state, 0),
                                     default="")
                current_state = self.statework_states.get(
                    (statework_id, permit.subject_ref))
                if current_state is None or normal_rank.get(current_state, -1) < minimum_rank:
                    raise PermissionError(
                        f"cannot finalize before StateWork {statework_id!r} reaches {required_state!r}")
                continue
            if not completion_states:
                continue
            state = self.statework_states.get((statework_id, permit.subject_ref))
            if self.runtime_mode == "combined_enforcing":
                if not self._authoritative_state_ready():
                    raise PermissionError("shared StateWork authority is unavailable for completion")
                state = self.statework_states.get((statework_id, permit.subject_ref))
            if state not in completion_states:
                raise PermissionError(
                    f"cannot finalize before StateWork {statework_id!r} reaches a terminal state")
        self._journal("task_finished", subject_ref=permit.subject_ref,
                      claim_digest=permit.claim_digest)
        return self._render_output(output, complete=True, **kwargs)


def _journal_identity_prefix(bundle: resolve_runtime.RuntimeBundle) -> str:
    values = (
        bundle.pinned.get("namespace_id", "default"),
        bundle.pinned.get("application_instance_id", "unknown-application"),
        bundle.pinned.get("agent_instance_id", bundle.pinned.get("agent_id", "agent")),
        bundle.task_id, bundle.pinned.get("attempt_id", "attempt-1"),
        bundle.pinned.get("interaction_id") or "",
    )
    return hashlib.sha256("|".join(str(value) for value in values).encode("utf-8")).hexdigest()[:32]


def start(bundle: resolve_runtime.RuntimeBundle, *,
          telemetry_sink: Optional[TelemetrySink] = None,
          telemetry_exporter: Optional[DurableTelemetryExporter] = None,
          behavioral_judge: Optional[BehavioralJudge] = None,
          state_authority: Optional[StateAuthority] = None,
          policy_revocation_authority: Optional[PolicyRevocationAuthority] = None,
          membership_authority: Optional["SharedWorkflowMembershipAuthority"] = None,
          operator_read_model: Optional[Any] = None,
          runtime_mode: str = "standalone_enforcing",
          telemetry_disabled: bool = False,
          tool_hooks: Optional[List[Callable[[Dict[str, Any]], Optional[GateDecision]]]] = None,
          final_hooks: Optional[List[Callable[[Dict[str, Any]], None]]] = None,
          action_registry: Optional[Mapping[str, ActionDescriptor]] = None,
          host_context: Optional[HostExecutionContext] = None,
          session_nonce: Optional[str] = None,
          session_id: Optional[str] = None,
          policy_epoch_record: Optional[PolicyEpochRecord] = None,
          resume: bool = False) -> RuntimeSession:
    """Inject selected runtime bytes, attach default host telemetry, and
    persist the immutable snapshot. Only tests may explicitly disable the
    sink; ordinary callers cannot silently turn it off."""
    if runtime_mode not in RUNTIME_MODES:
        raise ValueError(f"unknown runtime mode {runtime_mode!r}")
    supplied_context = host_context or HostExecutionContext()
    pinned_contracts = bundle.pinned.get("contracts", {})
    pinned_runtime_hash = pinned_contracts.get("runtime_behavior_hash")
    source_root = Path(bundle.statework_root or resolve_runtime.COW_ROOT)
    if pinned_runtime_hash != resolve_runtime.runtime_behavior_hash(source_root):
        raise resolve_runtime.SnapshotIntegrityError(
            "runtime enforcement code changed after policy composition")
    system_one_policy = SystemOnePolicy(**dict(bundle.pinned.get("system_one_policy") or {}))
    if dict(bundle.policy.system_one_policy) != system_one_policy.to_dict():
        raise resolve_runtime.SnapshotIntegrityError(
            "System-1 policy differs from the task's pinned policy")
    if policy_epoch_record is not None:
        if (not isinstance(policy_epoch_record, PolicyEpochRecord)
                or policy_epoch_record.epoch_index < 1
                or policy_epoch_record.parent_policy_hash == policy_epoch_record.child_policy_hash
                or policy_epoch_record.child_policy_hash != bundle.pinned.get("policy_hash")
                or policy_epoch_record.route_type != "flow"
                or policy_epoch_record.reason != "dynamic_replan"
                or policy_epoch_record.decision_source not in {
                    "metacognition", "operator", "learned_profile", "semantic_advisory"}
                or not policy_epoch_record.decision_ref
                or (policy_epoch_record.decision_source == "semantic_advisory"
                    and not policy_epoch_record.semantic_decision_ref)
                or policy_epoch_record.state_revision < 0):
            raise resolve_runtime.SnapshotIntegrityError(
                "child policy epoch record does not match the immutable runtime bundle")
    if behavioral_judge is not None:
        capability_method = getattr(behavioral_judge, "capability", None)
        if not callable(capability_method):
            raise ValueError("System-1 provider must expose a capability handshake")
        capability = capability_method()
        if isinstance(capability, Mapping):
            capability = SystemOneCapability(
                **{**dict(capability),
                   "supported_question_families": tuple(
                       capability.get("supported_question_families") or ())})
        if not isinstance(capability, SystemOneCapability):
            raise ValueError("System-1 provider returned an invalid capability handshake")
        pinned_capabilities = {
            "backend_id": system_one_policy.backend_id,
            "model_id": system_one_policy.model_id,
            "model_revision": system_one_policy.model_revision,
            "model_content_hash": system_one_policy.model_content_hash,
            "provider_protocol_hash": system_one_policy.provider_protocol_hash,
            "protocol_version": system_one_policy.protocol_version,
            "protocol_hash": system_one_policy.protocol_hash,
            "question_registry_hash": system_one_policy.question_registry_hash,
            "state_projection_schema_hash": system_one_policy.state_projection_schema_hash,
        }
        for field_name, expected in pinned_capabilities.items():
            if getattr(capability, field_name) != expected:
                raise ValueError(f"System-1 capability {field_name} differs from pinned policy")
        if capability.max_state_bytes < 32768:
            raise ValueError("System-1 provider cannot accept the maximum protocol frame")
        if not set(system_one_policy.enabled_question_families).issubset(
                capability.supported_question_families):
            raise ValueError("System-1 provider does not support every enabled question family")
        if capability.stateful_sessions and (
                not callable(getattr(behavioral_judge, "reset_identity", None))
                or not callable(getattr(behavioral_judge, "resync", None))):
            raise ValueError("stateful System-1 provider must implement reset and resync")
        provider_cache_size = getattr(behavioral_judge, "cache_size", None)
        if provider_cache_size is not None and int(provider_cache_size) != system_one_policy.cache_size:
            raise ValueError("injected System-1 cache size differs from pinned policy")
        provider_latency = getattr(behavioral_judge, "max_latency_ms", None)
        if (provider_latency is not None
                and int(provider_latency) > system_one_policy.max_latency_ms):
            raise ValueError("injected System-1 provider exceeds pinned latency cap")
    if (session_nonce is not None and supplied_context.session_nonce
            and session_nonce != supplied_context.session_nonce):
        raise ValueError("session nonce supplied by host context and resume request differ")
    session_nonce = supplied_context.session_nonce or session_nonce or f"nonce-{uuid.uuid4()}"
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", bundle.task_id)[:128] or "task"
    journal_root = resolve_runtime.runtime_state_dir() / "sessions"
    if resume and session_id is None:
        # Analytics identity is restored from the authority journal; it is
        # never regenerated from the security nonce or inferred from task ID.
        journal_db = journal_root / f"session-{_journal_identity_prefix(bundle)}-{safe_task}-{session_nonce}.sqlite3"
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
       system_one_judge=behavioral_judge,
       agent_instance_id=bundle.pinned.get("agent_instance_id", bundle.pinned.get("agent_id", "agent")),
       attempt_id=bundle.pinned.get("attempt_id", "attempt-1"),
       interaction_id=bundle.pinned.get("interaction_id"),
       parent_session_id=bundle.pinned.get("parent_session_id"),
       delegator_agent_id=bundle.pinned.get("delegator_agent_id"),
       delegate_agent_id=bundle.pinned.get("delegate_agent_id"),
       delegation_id=bundle.pinned.get("delegation_id"),
       role=bundle.pinned.get("role"),
       parent_decision_ref=bundle.pinned.get("parent_decision_ref"),
       shared_evidence_refs=tuple(bundle.pinned.get("shared_evidence_refs") or ()),
       delegation_topology=bundle.pinned.get("delegation_topology"),
       premise_digest=bundle.pinned.get("premise_digest"),
       failure_attribution=bundle.pinned.get("failure_attribution") or "none",
       adopted_profile_hashes=tuple(bundle.pinned.get("adopted_profile_hashes") or ()),
       shared_workflow_membership=bundle.pinned.get("shared_workflow_membership"),
       policy_revocation_revision=int(bundle.pinned.get("policy_revocation_revision", 0) or 0),
       revocation_mode=str(bundle.pinned.get("revocation_mode") or "deferred"),
       policy_epoch_record=policy_epoch_record)
    session.metacognitive_state.ensure_criteria(bundle.pinned.get("goal_contract") or {})
    session._ledger_token = object()
    session.ledger = EvidenceLedger(bundle.task_id, writer_token=session._ledger_token)
    session.execution_identity = ExecutionIdentity(
        namespace_id=bundle.pinned["namespace_id"],
        application_instance_id=bundle.pinned["application_instance_id"],
        agent_instance_id=session.agent_instance_id,
        session_id=session.session_id, task_id=bundle.task_id,
        attempt_id=session.attempt_id, interaction_id=session.interaction_id)
    verification_policy = ((bundle.pinned.get("goal_contract") or {}).get(
        "verification_policy") or {})
    session.convergence = ConvergenceController(identity={
        "namespace_id": session.execution_identity.namespace_id,
        "application_instance_id": session.execution_identity.application_instance_id,
        "agent_instance_id": session.execution_identity.agent_instance_id,
        "session_id": session.execution_identity.session_id,
        "task_id": session.execution_identity.task_id,
        "attempt_id": session.execution_identity.attempt_id,
        "interaction_id": session.execution_identity.interaction_id,
    }, max_recovery_epochs_without_progress=int(
        verification_policy.get("max_recovery_epochs_without_goal_progress", 3) or 3),
        max_same_approach_retries=int(
            verification_policy.get("max_same_approach_retries", 2) or 2))
    if runtime_mode == "combined_enforcing":
        if state_authority is None:
            raise ValueError("combined_enforcing requires an injected shared StateAuthority")
        if not bundle.stateworks:
            raise ValueError("combined_enforcing requires at least one StateWork-backed task")
        if isinstance(state_authority, CSWStateAuthority):
            membership = bundle.pinned.get("shared_workflow_membership")
            if membership:
                required = ("workflow_id", "statework_id", "subject_ref")
                if any(not membership.get(name) for name in required):
                    raise ValueError("shared workflow membership is incomplete")
                for field, expected in (
                    ("namespace_id", bundle.pinned["namespace_id"]),
                    ("application_id", bundle.pinned["application_id"]),
                    ("application_instance_id", bundle.pinned["application_instance_id"]),
                    ("subject_ref", bundle.policy.subject_ref),
                ):
                    if membership.get(field) != expected:
                        raise ValueError(f"shared workflow membership {field} differs from task identity")
                if membership_authority is None or not membership_authority.validate(
                        membership, session=session, operation="transition"):
                    raise PermissionError("shared workflow membership is not host-authorized")
                workflow_id = str(membership["workflow_id"])
            else:
                workflow_id = (
                    f"private:{bundle.pinned['namespace_id']}:"
                    f"{bundle.pinned['application_instance_id']}:{session.agent_instance_id}")
            state_authority = state_authority.bind_scope(
                namespace_id=bundle.pinned["namespace_id"],
                application_id=bundle.pinned["application_id"],
                application_instance_id=bundle.pinned["application_instance_id"],
                workflow_id=workflow_id)
            if bundle.pinned.get("shared_workflow_membership"):
                shared_branch_path = resolve_runtime.runtime_state_dir() / "shared-branches.sqlite3"
                session.shared_branch_ledger = SharedBranchLedger(shared_branch_path)
    if runtime_mode == "standalone_advisory" and state_authority is not None:
        raise ValueError("standalone_advisory cannot combine with shared StateWork authority")
    session.state_authority = state_authority
    session.policy_revocation_authority = policy_revocation_authority
    session.membership_authority = membership_authority
    session.operator_read_model = operator_read_model
    session.runtime_mode = runtime_mode
    if runtime_mode == "combined_enforcing" and not session._authoritative_state_ready():
        raise ValueError("shared StateWork authority could not resolve initial state")
    session._segment_status = {
        item.segment_id: (SegmentStatus.ACTIVE if index == 0 else SegmentStatus.PENDING)
        for index, item in enumerate(session.execution_segments)
    }
    journal_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(journal_root, 0o700)
    identity_digest = _journal_identity_prefix(bundle)
    session.journal_path = journal_root / f"session-{identity_digest}-{safe_task}-{session_nonce}.ndjson"
    session.journal_db_path = journal_root / f"session-{identity_digest}-{safe_task}-{session_nonce}.sqlite3"
    if not resume:
        if session.journal_path.exists() or session.journal_db_path.exists():
            raise FileExistsError(
                f"session journal already exists; use resume_task for {session_nonce!r}")
        session._journal("session_started", segment_id=session.current_segment.segment_id,
                         segment_index=session.current_segment_index,
                         authority_revision=session.authority_revision,
                         segment_status={key: value.value for key, value in session.segment_status.items()},
                         policy_epoch_record=(policy_epoch_record.to_dict()
                                              if policy_epoch_record else None))
    session.active_statework_ids = ({session.current_segment.statework_id}
                                    if session.current_segment.statework_id else set())
    session.lifecycle = LifecycleController(bundle)
    session.lifecycle.on_task_start({
        "task_complexity": (bundle.pinned.get("goal_contract") or {}).get(
            "task_complexity", "standard" if bundle.profile != "quick" else "trivial")})
    session.active_lifecycle = set(session.lifecycle.active)
    session.lifecycle_counts = dict(session.lifecycle.counts)
    if telemetry_sink is not None:
        session._emit = telemetry_sink
    elif not telemetry_disabled:
        digest = hashlib.sha256(
            session.execution_identity.storage_key().encode("utf-8")).hexdigest()[:24]
        store = (resolve_runtime.runtime_state_dir() / "telemetry" /
                 f"{digest}.ndjson")
        session._emit = HostTelemetry(
            bundle.task_id, store,
            expected_schema_hash=bundle.policy.behavior_event_schema_hash,
            expected_schema_version=bundle.policy.behavior_event_schema_version,
            namespace_id=bundle.pinned.get("namespace_id", "default"),
            application_id=bundle.pinned.get("application_id", "unknown-application"),
            application_version=bundle.pinned.get("application_version"),
            application_instance_id=bundle.pinned.get("application_instance_id"),
            provider_id=bundle.pinned.get("provider_id"),
            model_id=bundle.pinned.get("model_id", bundle.pinned.get("model")),
            model_revision=bundle.pinned.get("model_revision"),
            model_capability_hash=bundle.pinned.get("model_capability_hash"),
            harness_id=bundle.pinned.get("harness"),
            harness_version=bundle.pinned.get("harness_version"),
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
    if session._emit is not None and not resume:
        session._emit_runtime({
            "category": "decision", "event_type": "policy_composed",
            "subject": bundle.policy.subject_ref,
            "payload": {"summary": "host resolver froze task routing",
                        "result_class": "validated",
                        "policy_hash": bundle.pinned.get("policy_hash"),
                        "routing_profile_hash": bundle.policy.routing_profile_hash,
                        "intervention_assigned": ((bundle.guard_pack.get("routing_experiment") or {}).get("routing_cohort") == "treatment"),
                        "intervention_executed": False,
                        "effective_policy_hash": bundle.pinned.get("policy_hash")}})
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
    if telemetry_exporter is not None:
        object.__setattr__(session, "_telemetry_exporter", telemetry_exporter)
        telemetry_exporter.flush()
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
            epoch_data = record.get("policy_epoch_record")
            restored_epoch = (PolicyEpochRecord.from_mapping(epoch_data)
                              if isinstance(epoch_data, Mapping) else None)
            if restored_epoch is not None:
                if restored_epoch.child_policy_hash != session.bundle.pinned.get("policy_hash"):
                    raise PermissionError("child policy epoch journal differs from resumed bundle")
                if (session.policy_epoch_record is not None
                        and session.policy_epoch_record != restored_epoch):
                    raise PermissionError("supplied policy epoch differs from resumed journal")
                object.__setattr__(session, "policy_epoch_record", restored_epoch)
            elif session.policy_epoch_record is not None:
                raise PermissionError("resumed policy epoch is absent from the durable journal")
            statuses = record.get("segment_status") or {}
            for segment_id, status in statuses.items():
                session._segment_status[segment_id] = SegmentStatus(status)
        elif event_type == "segment_activated":
            session.current_segment_index = int(record["segment_index"])
            session._segment_status[record["segment_id"]] = SegmentStatus.ACTIVE
        elif event_type == "policy_revocation_observed":
            session.policy_revocation_revision = max(
                session.policy_revocation_revision,
                int(record.get("policy_revocation_revision", 0) or 0))
            session.revocation_mode = str(record.get("revocation_mode") or session.revocation_mode)
        elif event_type == "convergence_recorded":
            if session.convergence is not None:
                session.convergence.restore(record.get("convergence") or {})
        elif event_type == "route_decision_recorded":
            decision_id = str(record.get("decision_id") or record.get("route_decision_id") or "")
            if decision_id:
                session.route_decisions[decision_id] = dict(record.get("decision") or {})
        elif event_type == "route_outcome_recorded":
            event_id = str(record.get("event_id") or "")
            if event_id:
                session.route_outcomes[event_id] = dict(record.get("outcome") or {})
        elif event_type == "system_one_sequence":
            session.system_one_sequence = max(
                session.system_one_sequence, int(record.get("sequence", 0) or 0))
        elif event_type == "system_one_projection_changed":
            session.metacognitive_state.restore(record.get("snapshot") or {})
        elif event_type == "system_one_decision_recorded":
            decision = dict(record.get("decision") or {})
            decision_id = str(decision.get("decision_id") or "")
            if decision_id:
                session.system_one_decisions[decision_id] = decision
                session.system_one_sequence = max(
                    session.system_one_sequence, int(decision.get("sequence", 0) or 0))
        elif event_type == "system_one_outcome_recorded":
            session.system_one_outcomes.add(
                f"{record.get('decision_id') or ''}:{record.get('label') or ''}")
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
                "action_request_id": record.get("action_request_id"),
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
            transition = record.get("transition") or {}
            csw_progress_class = str(record.get("csw_progress_class")
                                      or transition.get("progress_class")
                                      or "LATERAL")
            if csw_progress_class in {"RECOVERY", "INVALIDATION", "REGRESSION"}:
                progress_class = "RECOVERY_PROGRESS"
            elif csw_progress_class == "TERMINAL":
                progress_class = "VERIFICATION_PROGRESS"
            else:
                progress_class = "DIAGNOSTIC_PROGRESS"
            session.metacognitive_state.record_csw_fact(
                statework_id=key[0],
                input_state=str(record["input_state"]),
                output_state=str(record["output_state"]),
                progress_class=csw_progress_class,
                workflow_metadata=transition)
            if session.convergence is not None:
                progress_digest = hashlib.sha256(json.dumps({
                    "statework_id": key[0], "subject_ref": key[1],
                    "input_state": record.get("input_state"),
                    "output_state": record.get("output_state"),
                    "edge_id": transition.get("edge_id"),
                }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                if csw_progress_class in {"RECOVERY", "INVALIDATION", "REGRESSION"}:
                    session.convergence.record_recovery_epoch(
                        cause_id="primary-objective")
                elif csw_progress_class == "TERMINAL":
                    evidence_refs = list(record.get("evidence_refs") or ())
                    session.convergence.record_progress(
                        cause_id="primary-objective", progress_digest=progress_digest,
                        progress_class="VERIFICATION_PROGRESS",
                        evidence_ref=evidence_refs[-1] if evidence_refs else None)
                elif csw_progress_class == "FORWARD_PROGRESS":
                    evidence_refs = list(record.get("evidence_refs") or ())
                    session.convergence.record_progress(
                        cause_id="primary-objective", progress_digest=progress_digest,
                        progress_class="DIAGNOSTIC_PROGRESS",
                        evidence_ref=evidence_refs[-1] if evidence_refs else None)
                else:
                    session.convergence.record_support_action(
                        cause_id="primary-objective")
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
                                      "completion-boundary-v1"),
                goal_contract_hash=str(record.get("goal_contract_hash") or ""),
                criterion_ids=tuple(record.get("criterion_ids") or ()),
                criterion_evidence_refs=tuple(
                    (str(key), tuple(refs))
                    for key, refs in record.get("criterion_evidence_refs") or ())))
        elif event_type == "attempt_descriptor_recorded":
            descriptor = AttemptDescriptor(**dict(record.get("descriptor") or {}))
            session.attempt_descriptors[descriptor.attempt_ref] = descriptor
            semantic_data = record.get("semantic_descriptor")
            if semantic_data:
                semantic_data = dict(semantic_data)
                semantic_data["affected_criterion_ids"] = tuple(
                    semantic_data.get("affected_criterion_ids") or ())
                session.attempt_semantics[descriptor.attempt_ref] = AttemptSemanticDescriptor(
                    **semantic_data)
        elif event_type == "attempt_failure_recorded":
            session.attempt_failures.add(str(record.get("attempt_ref") or ""))
    session.metacognitive_state.ensure_criteria(
        session.bundle.pinned.get("goal_contract") or {})
    current = session.current_segment
    session.active_statework_ids = ({current.statework_id}
                                    if current.statework_id else set())
    if session.journal_path is not None:
        os.chmod(session.journal_path, 0o600)
    if session.journal_db_path is not None and session.journal_db_path.exists():
        os.chmod(session.journal_db_path, 0o600)
    # A resumed model-visible behavior decision may have been based on stale
    # state. Keep the host reconstruction, clear transient influence, then
    # send a complete frame on the next eligible query.
    object.__setattr__(session, "semantic_signals", SemanticSignals())
    object.__setattr__(session, "semantic_signals_revision", -1)
    object.__setattr__(session, "metacognitive_decision", MetaDecision("DELIVER", "session restored"))
    session._pending_action_requests.clear()
    session._pending_retry_candidates.clear()
    if session.system_one_judge is not None:
        identity = SystemOneIdentity(
            namespace_id=session.execution_identity.namespace_id,
            application_id=str(session.bundle.pinned.get("application_id") or "unknown-application"),
            application_instance_id=session.execution_identity.application_instance_id,
            agent_id=str(session.bundle.pinned.get("agent_id") or "agent"),
            agent_instance_id=session.execution_identity.agent_instance_id,
            session_id=session.execution_identity.session_id,
            task_id=session.execution_identity.task_id,
            attempt_id=session.execution_identity.attempt_id,
            interaction_id=session.execution_identity.interaction_id)
        reset = getattr(session.system_one_judge, "reset_identity", None)
        if callable(reset):
            reset(identity)
    session._evaluate_metacognition(semantic=SemanticSignals())
    session._journal("system_one_resume_state_invalidated",
                     projector_restored=True, metacognitive_control_recomputed=True,
                     provider_resync_required=True)


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
    goal = session.bundle.pinned.get("goal_contract") or {}
    if goal:
        criteria = list(goal.get("acceptance_criteria") or ())
        required = [item for item in criteria if item.get("required", True)]
        parts.append(
            "===== TASK CONTRACT =====\n"
            f"Primary outcome: {goal.get('objective', '')}\n"
            f"Deliverable: {goal.get('deliverable_kind', 'unspecified')}; "
            f"work mode: {goal.get('work_mode', 'delivery')}\n"
            "Required criteria: " + ("; ".join(
                f"{item.get('criterion_id')}: {item.get('claim')}" for item in required
            ) or "none specified") + "\n"
            "Support work is subordinate to these criteria.\n"
            f"Contract hash: {session.bundle.pinned.get('goal_contract_hash', '')}")
        non_goals = list(goal.get("non_goals") or ())
        if non_goals:
            parts.append("Non-goals: " + "; ".join(str(item) for item in non_goals))
    else:
        parts.append(
            "===== TASK CONTRACT =====\n"
            "The host did not supply a structured task contract. Preserve the "
            "user's requested outcome; framework workflow is support work.")
    verification = goal.get("verification_policy") or {}
    support_limit = int(verification.get("support_actions_before_goal_reanchor", 5) or 5)
    if (goal.get("work_mode") == "delivery" and session.convergence is not None
            and session.convergence.needs_goal_reanchor(
                cause_id="primary-objective", support_limit=support_limit)):
        support_count = session.convergence.state("primary-objective").support_actions_since_goal_progress
        parts.append(
            "===== GOAL REANCHOR REQUIRED =====\n"
            f"No primary-deliverable progress in {support_count} support actions. "
            f"Primary outcome: {goal.get('objective', '')}\n"
            "Next action must advance a required criterion or establish a concrete blocker.")
    criterion_claims = {str(item.get("criterion_id")): str(item.get("claim") or "")
                        for item in goal.get("acceptance_criteria", ())}
    directive = render_metacognitive_directive(session._evaluate_metacognition())
    if directive:
        parts.append(directive +
                     "\nHost permissions, evidence requirements, retry limits, and completion checks remain authoritative.")
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
    if session.convergence is not None:
        blocks = [state.compact_state_block(
            max_recovery_epochs_without_progress=session.convergence.max_recovery_epochs_without_progress)
                  for state in session.convergence.states()]
        blocks = [block for block in blocks if block]
        if blocks:
            parts.append("\n===== CONVERGENCE =====\n" + "\n\n".join(blocks))
    action_hint = session._action_strategy_hint()
    if action_hint:
        parts.append(action_hint)
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
