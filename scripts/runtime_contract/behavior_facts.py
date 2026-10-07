"""Host-observed facts used by CFW local gates and standalone fallback.

These data contracts are independent of semantic providers. CFW keeps only
bounded facts needed for local enforcement and its standalone fallback. The
legacy provider protocol in ``system_one`` is retained for old artifacts;
live semantic assessment and policy belong to DigitalPsychology.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

FACT_SOURCES = frozenset({"host_observed", "csw_authoritative", "validator",
                          "operator", "model_proposed", "derived"})
CRITERION_STATUSES = frozenset({"unmet", "active", "partially_observed", "satisfied", "blocked"})
EVIDENCE_STATUSES = frozenset({"none", "available", "verified", "invalidated"})
ACTION_RELATIONS = frozenset({
    "advances_primary_goal", "removes_concrete_blocker",
    "required_verification", "diagnostic_support", "unrelated",
})


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
    relation_certainty: Optional[str] = None

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
        if self.relation_certainty is not None and self.relation_certainty not in {
                "explicit", "inferred", "unknown"}:
            raise ValueError("unsupported activity relation certainty")

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
                "progress_class": self.progress_class, "evidence_kinds": list(self.evidence_kinds),
                "relation_certainty": self.relation_certainty}


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
