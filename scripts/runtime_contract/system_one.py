"""Typed CFW-owned state and response contracts for System-1 providers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Optional, Protocol, Tuple

from .protocol_artifact import (PROTOCOL_HASH, PROTOCOL_VERSION,
                                QUESTION_REGISTRY, QUESTION_REGISTRY_HASH,
                                STATE_PROJECTION_SCHEMA_HASH)
from .task import SystemOnePolicy

FACT_SOURCES = frozenset({"host_observed", "csw_authoritative", "validator",
                          "operator", "model_proposed", "derived"})
CRITERION_STATUSES = frozenset({"unmet", "active", "partially_observed", "satisfied", "blocked"})
EVIDENCE_STATUSES = frozenset({"none", "available", "verified", "invalidated"})
ACTION_RELATIONS = frozenset(QUESTION_REGISTRY["action_relation"]["criteria"])
PROGRESS_RELATIONS = frozenset(QUESTION_REGISTRY["progress_relation"]["criteria"])
RETRY_RELATIONS = frozenset(QUESTION_REGISTRY["retry_relation"]["criteria"])
QUESTION_FIELDS = frozenset(QUESTION_REGISTRY)


def _bounded_text(value: Any, name: str, limit: int, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be bounded non-empty text")
    return value


def _digest(value: Any, name: str, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(
            char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def canonical_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SystemOneIdentity:
    namespace_id: str
    application_id: str
    application_instance_id: str
    agent_id: str
    agent_instance_id: str
    session_id: str
    task_id: str
    attempt_id: str
    interaction_id: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("namespace_id", "application_id", "application_instance_id", "agent_id",
                     "agent_instance_id", "session_id", "task_id", "attempt_id"):
            _bounded_text(getattr(self, name), name, 256)
        if self.interaction_id is not None:
            _bounded_text(self.interaction_id, "interaction_id", 256)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "namespace_id", "application_id", "application_instance_id", "agent_id",
            "agent_instance_id", "session_id", "task_id", "attempt_id", "interaction_id")}


@dataclass(frozen=True)
class CriterionState:
    criterion_id: str
    claim: str
    kind: str
    status: str = "unmet"
    evidence_status: str = "none"
    evidence_ref_digests: Tuple[str, ...] = ()
    last_progress_sequence: Optional[int] = None

    def __post_init__(self) -> None:
        _bounded_text(self.criterion_id, "criterion_id", 128)
        _bounded_text(self.claim, "criterion claim", 512)
        _bounded_text(self.kind, "criterion kind", 64)
        if self.status not in CRITERION_STATUSES or self.evidence_status not in EVIDENCE_STATUSES:
            raise ValueError("unsupported criterion or evidence status")
        if len(self.evidence_ref_digests) > 8:
            raise ValueError("too many criterion evidence references")
        for value in self.evidence_ref_digests:
            _digest(value, "evidence_ref_digest")
        if self.last_progress_sequence is not None and self.last_progress_sequence < 0:
            raise ValueError("last_progress_sequence must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"criterion_id": self.criterion_id, "claim": self.claim, "kind": self.kind,
                "status": self.status, "evidence_status": self.evidence_status,
                "evidence_ref_digests": list(self.evidence_ref_digests),
                "last_progress_sequence": self.last_progress_sequence}


@dataclass(frozen=True)
class ActivityFact:
    sequence: int
    activity_type: str
    source: str = "host_observed"
    action_class: Optional[str] = None
    host_declared_relation: Optional[str] = None
    target_kind: Optional[str] = None
    target_scope: Optional[str] = None
    target_ref_digest: Optional[str] = None
    proposed_purpose: Optional[str] = None
    proposed_purpose_source: str = "derived"
    affected_criterion_ids: Tuple[str, ...] = ()
    result_class: Optional[str] = None
    artifact_changed: Optional[bool] = None
    progress_class: Optional[str] = None
    evidence_kinds: Tuple[str, ...] = ()

    ACTIVITY_TYPES = frozenset({"action", "tool_result", "validation", "state_transition",
                                "workflow_progress", "retry", "goal_progress", "blocker",
                                "operator_correction"})
    PROGRESS_CLASSES = frozenset({"GOAL_PROGRESS", "BLOCKER_REMOVED", "DIAGNOSTIC_PROGRESS",
                                  "VERIFICATION_PROGRESS", "VALIDATION_EVIDENCE",
                                  "RECOVERY_PROGRESS", "WORKFLOW_PROGRESS"})

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("activity sequence must be positive")
        if self.activity_type not in self.ACTIVITY_TYPES:
            raise ValueError("unsupported activity type")
        if self.source not in FACT_SOURCES or self.proposed_purpose_source not in FACT_SOURCES:
            raise ValueError("unsupported fact provenance")
        for name, limit in (("action_class", 64), ("target_kind", 64), ("target_scope", 64),
                            ("proposed_purpose", 256),
                            ("result_class", 64)):
            _bounded_text(getattr(self, name), name, limit, optional=True)
        if self.host_declared_relation is not None and self.host_declared_relation not in (
                ACTION_RELATIONS | {"ambiguous"}):
            raise ValueError("unsupported host-declared goal relation")
        _digest(self.target_ref_digest, "target_ref_digest", optional=True)
        if len(self.affected_criterion_ids) > 8 or len(self.evidence_kinds) > 8:
            raise ValueError("activity fact exceeds bounded item count")
        for value in self.affected_criterion_ids:
            _bounded_text(value, "affected criterion id", 128)
        for value in self.evidence_kinds:
            _bounded_text(value, "evidence kind", 64)
        if self.progress_class is not None and self.progress_class not in self.PROGRESS_CLASSES:
            raise ValueError("unsupported activity progress class")

    def to_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "activity_type": self.activity_type,
                "source": self.source, "action_class": self.action_class,
                "host_declared_relation": self.host_declared_relation,
                "target_kind": self.target_kind, "target_scope": self.target_scope,
                "target_ref_digest": self.target_ref_digest,
                "proposed_purpose": self.proposed_purpose,
                "proposed_purpose_source": self.proposed_purpose_source,
                "affected_criterion_ids": list(self.affected_criterion_ids),
                "result_class": self.result_class, "artifact_changed": self.artifact_changed,
                "progress_class": self.progress_class, "evidence_kinds": list(self.evidence_kinds)}


@dataclass(frozen=True)
class WorkflowProgressFact:
    statework_id: str
    subject_ref: str
    engine_kind: str
    workflow_revision: int
    step_id: str
    work_class: str
    domain_effect: str
    progress_class: str
    current_position: Optional[str]
    previous_position: Optional[str]
    completed: bool
    evidence_refs: Tuple[str, ...] = ()
    source: str = "csw_authoritative"

    def __post_init__(self) -> None:
        for name, limit in (("statework_id", 64), ("progress_class", 32),
                            ("engine_kind", 64), ("step_id", 128),
                            ("work_class", 64), ("domain_effect", 64),
                            ("subject_ref", 256)):
            _bounded_text(getattr(self, name), name, limit)
        _bounded_text(self.current_position, "current_position", 128, optional=True)
        _bounded_text(self.previous_position, "previous_position", 128, optional=True)
        if self.progress_class not in {"FORWARD_PROGRESS", "LATERAL", "RECOVERY",
                                       "INVALIDATION", "REGRESSION", "TERMINAL"}:
            raise ValueError("unsupported StateWork progress class")
        if self.source != "csw_authoritative":
            raise ValueError("workflow facts must have CSW provenance")
        if (not isinstance(self.workflow_revision, int)
                or isinstance(self.workflow_revision, bool)
                or self.workflow_revision < 0):
            raise ValueError("workflow_revision must be a non-negative integer")
        if (not isinstance(self.completed, bool)
                or not isinstance(self.evidence_refs, (tuple, list))
                or len(self.evidence_refs) > 8):
            raise ValueError("workflow completion or evidence references are invalid")
        for value in self.evidence_refs:
            _bounded_text(value, "evidence reference", 256)
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def to_dict(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in (
            "statework_id", "subject_ref", "engine_kind", "workflow_revision", "step_id",
            "work_class", "domain_effect", "progress_class", "current_position",
            "previous_position", "completed", "source")}
        result["evidence_refs"] = list(self.evidence_refs)
        return result


# Compatibility name for callers that used the former provider projection.
WorkflowFact = WorkflowProgressFact


@dataclass(frozen=True)
class AttemptSemanticDescriptor:
    cause_class: str
    hypothesis_summary: str
    mechanism: str
    evidence_target: str
    planned_action_class: str
    planned_target_kind: str
    failed_result_class: str
    premise_digest: str
    action_plan_digest: str
    affected_criterion_ids: Tuple[str, ...] = ()
    source: str = "model_proposed"

    def __post_init__(self) -> None:
        for name, limit in (("cause_class", 64), ("hypothesis_summary", 256), ("mechanism", 256),
                            ("evidence_target", 128), ("planned_action_class", 64),
                            ("planned_target_kind", 64), ("failed_result_class", 64)):
            _bounded_text(getattr(self, name), name, limit)
        _digest(self.premise_digest, "premise_digest")
        _digest(self.action_plan_digest, "action_plan_digest")
        if len(self.affected_criterion_ids) > 8:
            raise ValueError("attempt descriptor references too many criteria")
        for value in self.affected_criterion_ids:
            _bounded_text(value, "affected criterion id", 128)
        if self.source not in {"model_proposed", "host_observed", "derived"}:
            raise ValueError("unsupported attempt descriptor provenance")

    def to_dict(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in (
            "cause_class", "hypothesis_summary", "mechanism", "evidence_target",
            "planned_action_class", "planned_target_kind", "failed_result_class",
            "premise_digest", "action_plan_digest", "source")}
        result["affected_criterion_ids"] = list(self.affected_criterion_ids)
        return result


@dataclass(frozen=True)
class SemanticState:
    task: Mapping[str, Any]
    situation: Mapping[str, Any]
    criteria: Tuple[CriterionState, ...]
    activity: Tuple[ActivityFact, ...]
    workflows: Tuple[WorkflowProgressFact, ...]
    convergence: Tuple[Tuple[str, int], ...]
    candidate: ActivityFact
    failed_approaches: Tuple[AttemptSemanticDescriptor, ...] = ()
    legal_routes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required_task = {"objective", "work_mode", "deliverable_kind", "task_complexity", "phase",
                         "active_segment", "active_statework", "active_flow", "non_goals"}
        if set(self.task) != required_task:
            raise ValueError("task context does not match the System-1 protocol")
        required_situation = {"current_state", "blockers", "actions_since_goal_progress",
                              "actions_since_artifact_change", "verification_actions_since_artifact_change",
                              "recovery_epochs", "repeated_branch_count"}
        if set(self.situation) != required_situation:
            raise ValueError("situation context does not match the System-1 protocol")
        _bounded_text(self.task["objective"], "objective", 1024)
        if self.task["work_mode"] not in {"delivery", "diagnose", "review", "research"}:
            raise ValueError("unsupported work mode")
        if self.task["task_complexity"] not in {"trivial", "standard", "complex"}:
            raise ValueError("unsupported task complexity")
        if self.task["deliverable_kind"] not in {
                "visual_ui", "code_behavior", "document", "architecture", "test_suite",
                "infrastructure", "analysis"}:
            raise ValueError("unsupported deliverable kind")
        for field in ("active_segment",):
            _bounded_text(self.task[field], field, 128)
        for field in ("phase", "active_statework", "active_flow"):
            _bounded_text(self.task[field], field, 64, optional=True)
        if len(self.task["non_goals"]) > 8 or len(self.criteria) > 8 or len(self.activity) > 16:
            raise ValueError("semantic state exceeds projection bounds")
        if len(self.workflows) > 8 or len(self.failed_approaches) > 4 or len(self.legal_routes) > 32:
            raise ValueError("semantic state exceeds projection bounds")

    def to_dict(self) -> dict[str, Any]:
        return {"task": dict(self.task), "situation": dict(self.situation),
                "criteria": [item.to_dict() for item in self.criteria],
                "activity": [item.to_dict() for item in self.activity],
                "workflows": [item.to_dict() for item in self.workflows],
                "convergence": dict(self.convergence), "candidate": self.candidate.to_dict(),
                "failed_approaches": [item.to_dict() for item in self.failed_approaches],
                "legal_routes": list(self.legal_routes)}


@dataclass(frozen=True)
class QuestionSpec:
    question_id: str
    family: str
    question_type: str
    instructions: str
    criteria: Tuple[Tuple[str, str], ...]

    def __post_init__(self) -> None:
        _bounded_text(self.question_id, "question_id", 64)
        _bounded_text(self.family, "question family", 64)
        _bounded_text(self.instructions, "question instructions", 512)
        if self.question_type not in {"choice", "score", "noul"}:
            raise ValueError("unsupported System-1 question type")
        if len(self.criteria) > 24:
            raise ValueError("question has too many options")
        for label, description in self.criteria:
            _bounded_text(label, "question option", 128)
            _bounded_text(description, "question option description", 512)

    def to_dict(self) -> dict[str, Any]:
        return {"question_id": self.question_id, "family": self.family,
                "type": self.question_type, "instructions": self.instructions,
                "criteria": {label: description for label, description in self.criteria}}


@dataclass(frozen=True)
class SystemOneFrame:
    identity: SystemOneIdentity
    request_id: str
    sequence: int
    state_digest: str
    question_bundle_digest: str
    request_digest: str
    policy_hash: str
    backend_id: str
    model_id: str
    model_revision: str
    model_content_hash: str
    provider_protocol_hash: str
    calibration_bundle_hash: str
    state: SemanticState
    questions: Tuple[QuestionSpec, ...]
    protocol_version: str = PROTOCOL_VERSION
    protocol_hash: str = PROTOCOL_HASH

    def __post_init__(self) -> None:
        _bounded_text(self.request_id, "request_id", 256)
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence must be positive")
        for name in ("state_digest", "question_bundle_digest", "request_digest", "policy_hash",
                     "model_content_hash", "provider_protocol_hash"):
            _digest(getattr(self, name), name)
        for name in ("backend_id", "model_id", "model_revision"):
            _bounded_text(getattr(self, name), name, 256)
        if self.calibration_bundle_hash:
            _digest(self.calibration_bundle_hash, "calibration_bundle_hash")
        if self.protocol_version != PROTOCOL_VERSION or self.protocol_hash != PROTOCOL_HASH:
            raise ValueError("unsupported System-1 protocol")
        if not self.questions or len(self.questions) > 8:
            raise ValueError("System-1 frame needs one to eight questions")
        state_payload = self.state.to_dict()
        question_payload = [item.to_dict() for item in self.questions]
        if canonical_hash(state_payload) != self.state_digest:
            raise ValueError("state_digest does not match semantic state")
        if canonical_hash(question_payload) != self.question_bundle_digest:
            raise ValueError("question_bundle_digest does not match questions")
        expected_request = canonical_hash({
            "identity": self.identity.to_dict(), "sequence": self.sequence,
            "state_digest": self.state_digest,
            "question_bundle_digest": self.question_bundle_digest,
            "policy_hash": self.policy_hash,
        })
        if expected_request != self.request_digest:
            raise ValueError("request_digest does not match request identity")
        if len(json.dumps(self.canonical_payload(), separators=(",", ":"), ensure_ascii=False)) > 32768:
            raise ValueError("System-1 frame exceeds 32 KiB")

    @property
    def question_ids(self) -> Tuple[str, ...]:
        return tuple(item.question_id for item in self.questions)

    def canonical_payload(self) -> dict[str, Any]:
        return {"protocol_version": self.protocol_version, "protocol_hash": self.protocol_hash,
                "identity": self.identity.to_dict(), "request_id": self.request_id,
                "sequence": self.sequence, "state_digest": self.state_digest,
                "question_bundle_digest": self.question_bundle_digest,
                "request_digest": self.request_digest, "policy_hash": self.policy_hash,
                "backend_id": self.backend_id, "model_id": self.model_id,
                "model_revision": self.model_revision,
                "model_content_hash": self.model_content_hash,
                "provider_protocol_hash": self.provider_protocol_hash,
                "question_registry_hash": QUESTION_REGISTRY_HASH,
                "state_projection_schema_hash": STATE_PROJECTION_SCHEMA_HASH,
                "calibration_bundle_hash": self.calibration_bundle_hash,
                "state": self.state.to_dict(),
                "questions": [item.to_dict() for item in self.questions]}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SystemOneFrame":
        """Parse the closed wire contract across independently installed repos."""
        fields = {"protocol_version", "protocol_hash", "identity", "request_id", "sequence",
                  "state_digest", "question_bundle_digest", "request_digest", "policy_hash",
                  "backend_id", "model_id", "model_revision", "model_content_hash",
                  "provider_protocol_hash", "question_registry_hash",
                  "state_projection_schema_hash", "calibration_bundle_hash", "state", "questions"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("malformed System-1 frame")
        if (value["question_registry_hash"] != QUESTION_REGISTRY_HASH
                or value["state_projection_schema_hash"] != STATE_PROJECTION_SCHEMA_HASH):
            raise ValueError("System-1 projection registry differs from this provider")
        identity_data = value["identity"]
        identity_fields = {"namespace_id", "application_id", "application_instance_id",
                           "agent_id", "agent_instance_id", "session_id", "task_id",
                           "attempt_id", "interaction_id"}
        if not isinstance(identity_data, Mapping) or set(identity_data) != identity_fields:
            raise ValueError("malformed System-1 identity")
        state_data = value["state"]
        state_fields = {"task", "situation", "criteria", "activity", "workflows",
                        "convergence", "candidate", "failed_approaches", "legal_routes"}
        if not isinstance(state_data, Mapping) or set(state_data) != state_fields:
            raise ValueError("malformed System-1 semantic state")
        task = dict(state_data["task"])
        situation = dict(state_data["situation"])
        task["non_goals"] = tuple(task["non_goals"])
        situation["blockers"] = tuple(situation["blockers"])

        def criterion(row: Mapping[str, Any]) -> CriterionState:
            return CriterionState(
                criterion_id=row["criterion_id"], claim=row["claim"], kind=row["kind"],
                status=row["status"], evidence_status=row["evidence_status"],
                evidence_ref_digests=tuple(row["evidence_ref_digests"]),
                last_progress_sequence=row["last_progress_sequence"])

        def activity(row: Mapping[str, Any]) -> ActivityFact:
            return ActivityFact(
                sequence=row["sequence"], activity_type=row["activity_type"],
                source=row["source"], action_class=row["action_class"],
                host_declared_relation=row["host_declared_relation"],
                target_kind=row["target_kind"], target_scope=row["target_scope"],
                target_ref_digest=row["target_ref_digest"],
                proposed_purpose=row["proposed_purpose"],
                proposed_purpose_source=row["proposed_purpose_source"],
                affected_criterion_ids=tuple(row["affected_criterion_ids"]),
                result_class=row["result_class"], artifact_changed=row["artifact_changed"],
                progress_class=row["progress_class"], evidence_kinds=tuple(row["evidence_kinds"]))

        def workflow(row: Mapping[str, Any]) -> WorkflowProgressFact:
            payload = dict(row)
            payload["evidence_refs"] = tuple(payload.get("evidence_refs") or ())
            return WorkflowProgressFact(**payload)

        def attempt(row: Mapping[str, Any]) -> AttemptSemanticDescriptor:
            payload = dict(row)
            payload["affected_criterion_ids"] = tuple(payload["affected_criterion_ids"])
            return AttemptSemanticDescriptor(**payload)

        candidate = activity(state_data["candidate"])
        state = SemanticState(
            task=task, situation=situation,
            criteria=tuple(criterion(row) for row in state_data["criteria"]),
            activity=tuple(activity(row) for row in state_data["activity"]),
            workflows=tuple(workflow(row) for row in state_data["workflows"]),
            convergence=tuple(sorted(state_data["convergence"].items())),
            candidate=candidate,
            failed_approaches=tuple(attempt(row) for row in state_data["failed_approaches"]),
            legal_routes=tuple(state_data["legal_routes"]))
        questions = []
        for row in value["questions"]:
            if not isinstance(row, Mapping) or set(row) != {
                    "question_id", "family", "type", "instructions", "criteria"}:
                raise ValueError("malformed System-1 question")
            questions.append(QuestionSpec(
                question_id=row["question_id"], family=row["family"],
                question_type=row["type"], instructions=row["instructions"],
                criteria=tuple(row["criteria"].items())))
        return cls(
            identity=SystemOneIdentity(**dict(identity_data)), request_id=value["request_id"],
            sequence=value["sequence"], state_digest=value["state_digest"],
            question_bundle_digest=value["question_bundle_digest"],
            request_digest=value["request_digest"], policy_hash=value["policy_hash"],
            backend_id=value["backend_id"], model_id=value["model_id"],
            model_revision=value["model_revision"],
            model_content_hash=value["model_content_hash"],
            provider_protocol_hash=value["provider_protocol_hash"],
            calibration_bundle_hash=value["calibration_bundle_hash"],
            state=state, questions=tuple(questions),
            protocol_version=value["protocol_version"], protocol_hash=value["protocol_hash"])


@dataclass(frozen=True)
class QuestionProbability:
    label: str
    probability: float

    def __post_init__(self) -> None:
        _bounded_text(self.label, "probability label", 128)
        if not isinstance(self.probability, (int, float)) or not math.isfinite(self.probability):
            raise ValueError("probability must be finite")
        if not 0 <= float(self.probability) <= 1:
            raise ValueError("probability must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "probability": float(self.probability)}


@dataclass(frozen=True)
class QuestionAnswer:
    question_id: str
    selected: str
    raw_probabilities: Tuple[QuestionProbability, ...]
    calibrated_probabilities: Optional[Tuple[QuestionProbability, ...]] = None
    calibration_hash: Optional[str] = None

    def __post_init__(self) -> None:
        _bounded_text(self.question_id, "question_id", 64)
        _bounded_text(self.selected, "selected answer", 128)
        labels = [item.label for item in self.raw_probabilities]
        if not labels or len(labels) != len(set(labels)) or self.selected not in labels:
            raise ValueError("raw probabilities must uniquely cover the selected answer")
        if abs(sum(item.probability for item in self.raw_probabilities) - 1.0) > 0.02:
            raise ValueError("raw probabilities must sum to one")
        if self.calibrated_probabilities is not None:
            calibrated = [item.label for item in self.calibrated_probabilities]
            if calibrated != labels:
                raise ValueError("calibrated probabilities must preserve option order")
            if abs(sum(item.probability for item in self.calibrated_probabilities) - 1.0) > 0.02:
                raise ValueError("calibrated probabilities must sum to one")
            _digest(self.calibration_hash, "calibration_hash")
        elif self.calibration_hash is not None:
            raise ValueError("calibration hash supplied without calibrated probabilities")

    @property
    def raw_confidence(self) -> float:
        return next(item.probability for item in self.raw_probabilities if item.label == self.selected)

    @property
    def calibrated_confidence(self) -> Optional[float]:
        if self.calibrated_probabilities is None:
            return None
        return next(item.probability for item in self.calibrated_probabilities
                    if item.label == self.selected)

    def to_dict(self) -> dict[str, Any]:
        return {"question_id": self.question_id, "selected": self.selected,
                "raw_probabilities": [item.to_dict() for item in self.raw_probabilities],
                "calibrated_probabilities": ([item.to_dict() for item in self.calibrated_probabilities]
                                              if self.calibrated_probabilities is not None else None),
                "calibration_hash": self.calibration_hash}


@dataclass(frozen=True)
class SystemOneJudgment:
    identity: SystemOneIdentity
    request_id: str
    sequence: int
    state_digest: str
    question_bundle_digest: str
    request_digest: str
    policy_hash: str
    backend_id: str
    model_id: str
    model_revision: str
    model_content_hash: str
    provider_protocol_hash: str
    calibration_bundle_hash: str
    answers: Tuple[QuestionAnswer, ...]
    protocol_version: str = PROTOCOL_VERSION
    protocol_hash: str = PROTOCOL_HASH

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION or self.protocol_hash != PROTOCOL_HASH:
            raise ValueError("unsupported System-1 protocol")
        for name in ("request_id", "backend_id", "model_id", "model_revision"):
            _bounded_text(getattr(self, name), name, 256)
        for name in ("state_digest", "question_bundle_digest", "request_digest", "policy_hash",
                     "model_content_hash", "provider_protocol_hash"):
            _digest(getattr(self, name), name)
        if self.calibration_bundle_hash:
            _digest(self.calibration_bundle_hash, "calibration_bundle_hash")
        if self.sequence < 1 or not self.answers or len(self.answers) > 8:
            raise ValueError("judgment sequence or answer count is invalid")
        ids = [answer.question_id for answer in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("judgment question answers must be unique")

    def answer(self, question_id: str) -> Optional[QuestionAnswer]:
        return next((answer for answer in self.answers if answer.question_id == question_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {"protocol_version": self.protocol_version, "protocol_hash": self.protocol_hash,
                "identity": self.identity.to_dict(), "request_id": self.request_id,
                "sequence": self.sequence, "state_digest": self.state_digest,
                "question_bundle_digest": self.question_bundle_digest,
                "request_digest": self.request_digest, "policy_hash": self.policy_hash,
                "backend_id": self.backend_id, "model_id": self.model_id,
                "model_revision": self.model_revision,
                "model_content_hash": self.model_content_hash,
                "provider_protocol_hash": self.provider_protocol_hash,
                "calibration_bundle_hash": self.calibration_bundle_hash,
                "answers": [answer.to_dict() for answer in self.answers]}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SystemOneJudgment":
        allowed = {"protocol_version", "protocol_hash", "identity", "request_id", "sequence",
                   "state_digest", "question_bundle_digest", "request_digest", "policy_hash",
                   "backend_id", "model_id", "model_revision", "model_content_hash",
                   "provider_protocol_hash", "calibration_bundle_hash", "answers"}
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise ValueError("malformed System-1 judgment")
        identity_data = value["identity"]
        identity_keys = {"namespace_id", "application_id", "application_instance_id", "agent_id",
                         "agent_instance_id", "session_id", "task_id", "attempt_id", "interaction_id"}
        if not isinstance(identity_data, Mapping) or set(identity_data) != identity_keys:
            raise ValueError("malformed System-1 identity")
        answers = []
        answer_keys = {"question_id", "selected", "raw_probabilities", "calibrated_probabilities",
                       "calibration_hash"}
        for item in value["answers"]:
            if not isinstance(item, Mapping) or set(item) != answer_keys:
                raise ValueError("malformed per-question answer")
            raw = tuple(QuestionProbability(**dict(probability))
                        for probability in item["raw_probabilities"])
            calibrated_data = item["calibrated_probabilities"]
            calibrated = (tuple(QuestionProbability(**dict(probability))
                                for probability in calibrated_data)
                          if calibrated_data is not None else None)
            answers.append(QuestionAnswer(
                question_id=item["question_id"], selected=item["selected"],
                raw_probabilities=raw, calibrated_probabilities=calibrated,
                calibration_hash=item["calibration_hash"]))
        payload = {key: value[key] for key in allowed - {"identity", "answers"}}
        return cls(identity=SystemOneIdentity(**dict(identity_data)), answers=tuple(answers), **payload)


@dataclass(frozen=True)
class SystemOneCapability:
    backend_id: str
    model_id: str
    model_revision: str
    model_content_hash: str
    provider_protocol_hash: str
    protocol_version: str
    protocol_hash: str
    question_registry_hash: str
    state_projection_schema_hash: str
    supported_question_families: Tuple[str, ...]
    max_state_bytes: int
    stateful_sessions: bool
    max_concurrent_requests: int = 4

    def __post_init__(self) -> None:
        for name in ("backend_id", "model_id", "model_revision"):
            _bounded_text(getattr(self, name), name, 256)
        for name in ("model_content_hash", "provider_protocol_hash", "protocol_hash",
                     "question_registry_hash", "state_projection_schema_hash"):
            _digest(getattr(self, name), name)
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("provider protocol version mismatch")
        if not 1 <= self.max_state_bytes <= 32768:
            raise ValueError("provider max_state_bytes is out of bounds")
        if not 1 <= self.max_concurrent_requests <= 64:
            raise ValueError("provider max_concurrent_requests is out of bounds")
        if not set(self.supported_question_families).issubset(QUESTION_FIELDS):
            raise ValueError("provider advertises an unknown question family")

    def to_dict(self) -> dict[str, Any]:
        return {"backend_id": self.backend_id, "model_id": self.model_id,
                "model_revision": self.model_revision, "model_content_hash": self.model_content_hash,
                "provider_protocol_hash": self.provider_protocol_hash,
                "protocol_version": self.protocol_version, "protocol_hash": self.protocol_hash,
                "question_registry_hash": self.question_registry_hash,
                "state_projection_schema_hash": self.state_projection_schema_hash,
                "supported_question_families": list(self.supported_question_families),
                "max_state_bytes": self.max_state_bytes,
                "stateful_sessions": self.stateful_sessions,
                "max_concurrent_requests": self.max_concurrent_requests}


class BehavioralJudge(Protocol):
    """Host-injected internal provider; never registered as a model-facing tool."""

    def capability(self) -> SystemOneCapability: ...

    def judge(self, frame: SystemOneFrame, *, mode: str) -> Any: ...

    def reset_identity(self, identity: SystemOneIdentity) -> None: ...

    def resync(self, frame: SystemOneFrame) -> None: ...


__all__ = ["SystemOnePolicy", "SystemOneIdentity", "CriterionState", "ActivityFact",
           "WorkflowProgressFact", "WorkflowFact", "AttemptSemanticDescriptor", "SemanticState", "QuestionSpec",
           "QuestionProbability", "QuestionAnswer", "SystemOneFrame", "SystemOneJudgment",
           "SystemOneCapability", "BehavioralJudge", "QUESTION_REGISTRY",
           "QUESTION_REGISTRY_HASH", "STATE_PROJECTION_SCHEMA_HASH", "canonical_hash"]
