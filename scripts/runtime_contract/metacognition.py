"""Provider-independent, host-owned metacognitive working state."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from .standalone_attention import StandaloneAttentionPolicy
from .system_one import (
    ActivityFact,
    AttemptSemanticDescriptor,
    CriterionState,
    WorkflowProgressFact,
)


@dataclass(frozen=True)
class BlockerFact:
    blocker_id: str
    summary: str
    criterion_ids: tuple[str, ...] = ()
    evidence_ref: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"blocker_id": self.blocker_id, "summary": self.summary,
                "criterion_ids": list(self.criterion_ids),
                "evidence_ref": self.evidence_ref}


@dataclass(frozen=True)
class CriterionEvidenceBinding:
    criterion_id: str
    evidence_ref: str
    evidence_kind: str
    authority: str
    artifact_revision: str | None = None

    def __post_init__(self) -> None:
        for name in ("criterion_id", "evidence_ref", "evidence_kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"criterion evidence {name} must be bounded text")
        if self.authority not in {"validator", "operator", "csw_authoritative", "host_observed"}:
            raise ValueError("criterion evidence binding requires a host authority")
        if self.artifact_revision is not None and len(self.artifact_revision) > 256:
            raise ValueError("artifact revision reference is too long")


@dataclass(frozen=True)
class SemanticSignals:
    """Independent semantic observations; they carry no execution mode or authority."""

    action_relation: str | None = None
    progress_relation: str | None = None
    equivalent_retry: bool | None = None
    possible_goal_drift: bool | None = None
    route_preference: str | None = None
    source_decisions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"action_relation": self.action_relation,
                "progress_relation": self.progress_relation,
                "equivalent_retry": self.equivalent_retry,
                "possible_goal_drift": self.possible_goal_drift,
                "route_preference": self.route_preference,
                "source_decisions": list(self.source_decisions)}


@dataclass(frozen=True)
class SemanticSignalRecord:
    question_family: str
    scenario: str
    criterion_id: str | None
    cause_id: str | None
    candidate_digest: str
    metacognitive_revision: int
    value: Any
    confidence: float | None
    source_decision_id: str

    def to_dict(self) -> dict[str, object]:
        return {"question_family": self.question_family, "scenario": self.scenario,
                "criterion_id": self.criterion_id, "cause_id": self.cause_id,
                "candidate_digest": self.candidate_digest,
                "metacognitive_revision": self.metacognitive_revision,
                "value": self.value, "confidence": self.confidence,
                "source_decision_id": self.source_decision_id}


class SemanticSignalStore:
    """Bounded task-local semantic signals with explicit decision scope."""
    _FIELDS: ClassVar[dict[str, str]] = {
        "action_relation": "action_relation",
        "progress_relation": "progress_relation",
        "retry_relation": "equivalent_retry",
        "goal_drift": "possible_goal_drift",
        "route_preference": "route_preference",
    }

    def __init__(self, *, limit: int = 128) -> None:
        if not 1 <= limit <= 512:
            raise ValueError("semantic signal store limit must be between 1 and 512")
        self.limit = limit
        self._records: dict[tuple[Any, ...], SemanticSignalRecord] = {}

    @property
    def records(self) -> tuple[SemanticSignalRecord, ...]:
        return tuple(self._records.values())

    def record(self, *, question_family: str, value: Any, scenario: str,
               candidate_digest: str, metacognitive_revision: int,
               source_decision_id: str, criterion_id: str | None = None,
               cause_id: str | None = None,
               confidence: float | None = None) -> None:
        if question_family not in self._FIELDS:
            raise ValueError("semantic signal family is not registered")
        if not scenario or not source_decision_id or not candidate_digest:
            raise ValueError("semantic signal requires scenario, candidate, and decision identities")
        if len(candidate_digest) != 64 or metacognitive_revision < 0:
            raise ValueError("semantic signal candidate digest or revision is invalid")
        if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("semantic signal confidence must be between zero and one")
        record = SemanticSignalRecord(
            question_family=question_family, scenario=scenario[:128],
            criterion_id=criterion_id[:128] if criterion_id else None,
            cause_id=cause_id[:128] if cause_id else None,
            candidate_digest=candidate_digest,
            metacognitive_revision=int(metacognitive_revision), value=value,
            confidence=float(confidence) if confidence is not None else None,
            source_decision_id=source_decision_id[:256])
        key = (record.question_family, record.scenario, record.criterion_id,
               record.cause_id, record.candidate_digest, record.metacognitive_revision)
        self._records[key] = record
        while len(self._records) > self.limit:
            self._records.pop(next(iter(self._records)))

    def invalidate(self, *, question_families: set[str] | None = None,
                   scenario: str | None = None, criterion_id: str | None = None,
                   cause_id: str | None = None,
                   candidate_digest: str | None = None) -> int:
        removed = []
        for key, item in self._records.items():
            if question_families is not None and item.question_family not in question_families:
                continue
            if scenario is not None and item.scenario != scenario:
                continue
            if criterion_id is not None and item.criterion_id != criterion_id:
                continue
            if cause_id is not None and item.cause_id != cause_id:
                continue
            if candidate_digest is not None and item.candidate_digest != candidate_digest:
                continue
            removed.append(key)
        for key in removed:
            del self._records[key]
        return len(removed)

    def snapshot(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in self._records.values()]

    def restore(self, records: Sequence[Mapping[str, Any]]) -> None:
        if len(records) > self.limit:
            raise ValueError("semantic signal snapshot exceeds configured bound")
        self._records.clear()
        for row in records:
            self.record(
                question_family=str(row.get("question_family") or ""),
                value=row.get("value"), scenario=str(row.get("scenario") or ""),
                candidate_digest=str(row.get("candidate_digest") or ""),
                metacognitive_revision=int(row.get("metacognitive_revision", -1)),
                source_decision_id=str(row.get("source_decision_id") or ""),
                criterion_id=(str(row["criterion_id"])
                              if row.get("criterion_id") else None),
                cause_id=(str(row["cause_id"]) if row.get("cause_id") else None),
                confidence=(float(row["confidence"])
                            if row.get("confidence") is not None else None))

    def current(self, *, metacognitive_revision: int,
                question_families: set[str] | None = None) -> SemanticSignals:
        eligible = [item for item in self._records.values()
                    if item.metacognitive_revision == int(metacognitive_revision)
                    and (question_families is None
                         or item.question_family in question_families)]
        if not eligible:
            return SemanticSignals()
        # Query families from different candidates/scenarios must not be
        # accidentally fused. Only records for the most recently observed
        # candidate context are combined.
        latest = eligible[-1]
        context = [item for item in eligible
                   if (item.scenario, item.candidate_digest, item.cause_id)
                   == (latest.scenario, latest.candidate_digest, latest.cause_id)]
        values: dict[str, Any] = {}
        sources: list[str] = []
        for item in context:
            values[self._FIELDS[item.question_family]] = item.value
            sources.append(item.source_decision_id)
        return SemanticSignals(**values,
                               source_decisions=tuple(dict.fromkeys(sources))[:8])


@dataclass(frozen=True)
class VerificationObligation:
    criterion_id: str
    artifact_revision: int
    required_strength: str
    evidence_kind: str | None = None
    status: str = "PENDING"

    def __post_init__(self) -> None:
        if not self.criterion_id or len(self.criterion_id) > 128:
            raise ValueError("verification obligation requires a bounded criterion id")
        if self.artifact_revision < 0 or not self.required_strength:
            raise ValueError("verification obligation revision and strength are required")
        if self.status not in {"PENDING", "SATISFIED", "INVALIDATED"}:
            raise ValueError("unsupported verification obligation status")

    def to_dict(self) -> dict[str, object]:
        return {"criterion_id": self.criterion_id,
                "artifact_revision": self.artifact_revision,
                "required_strength": self.required_strength,
                "evidence_kind": self.evidence_kind, "status": self.status}


@dataclass(frozen=True)
class CompletionReadiness:
    required_criteria_satisfied: bool
    boundary_evidence_available: bool
    unresolved_contradictions: bool
    active_stateworks_complete: bool
    pending_verification_obligations: int

    @property
    def ready(self) -> bool:
        return (self.required_criteria_satisfied and self.boundary_evidence_available
                and not self.unresolved_contradictions
                and self.active_stateworks_complete
                and self.pending_verification_obligations == 0)


@dataclass(frozen=True)
class MetaDecision:
    mode: str
    reason: str
    active_criterion_ids: tuple[str, ...] = ()
    goal_reanchor_required: bool = False
    semantic_attention_note: str | None = None
    source_decisions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"DELIVER", "DIAGNOSE", "VERIFY", "RECOVER", "REPLAN",
                             "BLOCKED", "REINSPECT", "READY_TO_COMPLETE"}:
            raise ValueError("unsupported metacognitive decision")
        if len(self.active_criterion_ids) > 8 or len(self.source_decisions) > 8:
            raise ValueError("metacognitive decision exceeds bounded references")

    def to_dict(self) -> dict[str, object]:
        return {"mode": self.mode, "reason": self.reason,
                "active_criterion_ids": list(self.active_criterion_ids),
                "goal_reanchor_required": self.goal_reanchor_required,
                "semantic_attention_note": self.semantic_attention_note,
                "source_decisions": list(self.source_decisions)}


class MetacognitiveController:
    """Make execution-focus decisions from CFW state and hard host policies."""

    def evaluate(self, state: MetacognitiveState, *,
                 support_actions_since_progress: int,
                 support_action_limit: int,
                 total_actions_since_progress: int,
                 total_action_limit: int,
                 recovery_epochs_since_progress: int,
                 max_recovery_epochs: int,
                 same_approach_retries: int,
                 max_same_approach_retries: int,
                 unresolved_contradiction: bool = False,
                 goal_contract_anchor_valid: bool = True,
                 semantic: SemanticSignals | None = None,
                 completion_readiness: CompletionReadiness | None = None) -> MetaDecision:
        active = state.active_attention_window()
        if unresolved_contradiction:
            return MetaDecision("REINSPECT", "unresolved operator contradiction", active)
        if not goal_contract_anchor_valid:
            return MetaDecision(
                "REPLAN", "host goal contract is missing or no longer matches its pinned identity",
                active, goal_reanchor_required=True)
        if recovery_epochs_since_progress >= max_recovery_epochs:
            if StandaloneAttentionPolicy.runnable(state.criteria):
                return MetaDecision(
                    "REPLAN", "current recovery budget exhausted; yield to runnable criteria",
                    active)
            return MetaDecision("BLOCKED", "hard recovery budget exhausted", active)
        if support_actions_since_progress >= support_action_limit:
            if state.progress_redirect_guidance_pending or not state.progress_redirected:
                return MetaDecision(
                    "REPLAN", "support-action budget reached; redirect to an unfinished criterion",
                    active)
            return MetaDecision(
                "DELIVER", "progress redirect issued; continue with a runnable criterion",
                active)
        if total_actions_since_progress >= total_action_limit:
            if state.progress_redirect_guidance_pending or not state.progress_redirected:
                return MetaDecision(
                    "REPLAN", "committed-action budget reached without goal evidence",
                    active)
            return MetaDecision(
                "DELIVER", "progress redirect issued; continue with a runnable criterion",
                active)
        if same_approach_retries >= max_same_approach_retries:
            return MetaDecision("RECOVER", "repeated approach reached its hard limit", active)
        if state.blockers:
            return MetaDecision("DIAGNOSE", "host-observed blocker remains", active)
        pending_obligations = state.pending_verification_obligations
        if pending_obligations:
            return MetaDecision("VERIFY", "explicit criterion verification obligation is pending",
                                active)
        note = None
        sources: tuple[str, ...] = ()
        if semantic is not None:
            sources = semantic.source_decisions[:8]
            if semantic.possible_goal_drift:
                note = "A semantic signal suggests possible drift; keep the next action tied to an unfinished criterion."
            elif semantic.action_relation is not None:
                note = (f"Semantic action-relevance hint: {semantic.action_relation}; "
                        "use it to guide attention, while host progress and verification rules remain authoritative.")
            elif semantic.progress_relation is not None:
                note = (f"Semantic outcome hint: {semantic.progress_relation}; "
                        "only host-bound criterion evidence changes hard progress.")
        if active:
            return MetaDecision("DELIVER", "required criterion remains unfinished", active,
                                semantic_attention_note=note, source_decisions=sources)
        if completion_readiness is not None and completion_readiness.ready:
            return MetaDecision("READY_TO_COMPLETE", "host completion prerequisites are ready",
                                semantic_attention_note=note, source_decisions=sources)
        if (completion_readiness is not None
                and not completion_readiness.active_stateworks_complete):
            return MetaDecision("DELIVER", "active workflow still has required steps",
                                semantic_attention_note=note, source_decisions=sources)
        return MetaDecision("VERIFY", "completion readiness still requires host evidence",
                            semantic_attention_note=note, source_decisions=sources)


class MetacognitiveState:
    """Keeps host-observed task state independent of any semantic provider."""

    _IMPORTANT = frozenset({"goal_progress", "blocker", "retry", "validation",
                            "operator_correction"})

    def __init__(self, *, activity_limit: int = 8, important_limit: int = 8,
                 workflow_limit: int = 8, failed_approach_limit: int = 4) -> None:
        if not (1 <= activity_limit <= 8 and 1 <= important_limit <= 8
                and 1 <= workflow_limit <= 8 and 1 <= failed_approach_limit <= 4):
            raise ValueError("System-1 projection limits exceed the safe bound")
        self._ordinary = deque(maxlen=activity_limit)
        self._important: dict[str, ActivityFact] = {}
        self._workflows: dict[tuple[str, str], WorkflowProgressFact] = {}
        self._failed_approaches = deque(maxlen=failed_approach_limit)
        self._important_limit = important_limit
        self._workflow_limit = workflow_limit
        self._next_sequence = 0
        self._criteria: dict[str, CriterionState] = {}
        self._criterion_action_counts: dict[str, int] = {}
        self._criterion_retry_counts: dict[str, int] = {}
        self._criterion_redirect_counts: dict[str, int] = {}
        self._criterion_last_action_sequence: dict[str, int] = {}
        self._progress_redirect_epoch = 0
        self._last_progress_redirect_action: str | None = None
        self._last_progress_redirect_criterion: str | None = None
        self._progress_redirect_yield_target: str | None = None
        self._progress_redirect_guidance_pending = False
        self._blockers: dict[str, BlockerFact] = {}
        self._actions_since_goal_progress = 0
        self._actions_since_artifact_change: int | None = None
        self._verification_actions_since_artifact_change = 0
        self._artifact_revision = 0
        self._verification_counts: dict[str, int] = {}
        self._verification_fingerprints: dict[str, int] = {}
        self._goal_directed_actions_since_goal_progress = 0
        self._recovery_epochs = 0
        self._repeated_branch_count = 0
        self._logical_revision = 0
        self._verification_requirements: dict[str, tuple[str, str | None]] = {}
        self._verification_obligations: dict[str, VerificationObligation] = {}

    @property
    def criteria(self) -> tuple[CriterionState, ...]:
        return tuple(sorted(self._criteria.values(), key=lambda item: item.criterion_id))

    @property
    def criterion_retry_counts(self) -> Mapping[str, int]:
        return dict(self._criterion_retry_counts)

    @property
    def criterion_last_action_sequence(self) -> Mapping[str, int]:
        return dict(self._criterion_last_action_sequence)

    def active_attention_window(self) -> tuple[str, ...]:
        return StandaloneAttentionPolicy.select(
            self.criteria, retry_counts=self._criterion_retry_counts,
            last_action_sequence=self._criterion_last_action_sequence,
            redirect_counts=self._criterion_redirect_counts)

    @property
    def progress_redirected(self) -> bool:
        return any(self._criterion_redirect_counts.values())

    @property
    def progress_redirect_guidance_pending(self) -> bool:
        return self._progress_redirect_guidance_pending

    @property
    def progress_redirect_epoch(self) -> int:
        return self._progress_redirect_epoch

    def record_progress_redirect(self, *, action_digest: str,
                                 criterion_id: str | None,
                                 max_redirects_per_criterion: int = 2) -> str:
        """Emit bounded progress redirects, then yield the stuck criterion.

        An identical action does not restate the same redirect. A changed
        action may trigger one more redirect for the criterion; that second
        redirect rotates it behind other runnable criteria and lets the new
        action proceed.
        """
        if (not isinstance(action_digest, str) or not action_digest
                or len(action_digest) > 128
                or not 1 <= max_redirects_per_criterion <= 8):
            raise ValueError("progress redirect identity or limit is invalid")
        selected_criterion = (criterion_id if criterion_id in self._criteria
                              else next(iter(self.active_attention_window()), ""))
        key = selected_criterion or "__task__"
        if (self._last_progress_redirect_action == action_digest
                and self._last_progress_redirect_criterion == key):
            return "repeat"
        count = self._criterion_redirect_counts.get(key, 0)
        if count >= max_redirects_per_criterion:
            return "limit"
        count += 1
        self._criterion_redirect_counts[key] = count
        self._last_progress_redirect_action = action_digest
        self._last_progress_redirect_criterion = key
        self._logical_revision += 1
        if count >= max_redirects_per_criterion:
            next_criteria = tuple(item for item in self.active_attention_window()
                                  if item != key)
            if next_criteria:
                self._progress_redirect_yield_target = next_criteria[0]
                self._progress_redirect_guidance_pending = True
                return "yield"
            return "limit"
        self._progress_redirect_guidance_pending = True
        return "redirect"

    def consume_progress_redirect_yield(self, criterion_id: str | None) -> bool:
        """Allow one action after attention has yielded to this criterion."""
        if (not criterion_id or criterion_id != self._progress_redirect_yield_target):
            return False
        self._progress_redirect_yield_target = None
        self._logical_revision += 1
        return True

    def take_progress_redirect_guidance(self) -> str | None:
        """Consume one host-generated redirect notice for model-visible context."""
        if not self._progress_redirect_guidance_pending:
            return None
        self._progress_redirect_guidance_pending = False
        self._logical_revision += 1
        return self._last_progress_redirect_criterion

    def _reset_progress_redirects(self) -> None:
        self._progress_redirect_epoch += 1
        self._criterion_redirect_counts.clear()
        self._last_progress_redirect_action = None
        self._last_progress_redirect_criterion = None
        self._progress_redirect_yield_target = None
        self._progress_redirect_guidance_pending = False

    @property
    def workflows(self) -> tuple[WorkflowProgressFact, ...]:
        return tuple(sorted(self._workflows.values(),
                            key=lambda item: (item.statework_id, item.subject_ref or "")))

    @property
    def failed_approaches(self) -> tuple[AttemptSemanticDescriptor, ...]:
        return tuple(self._failed_approaches)

    @property
    def recent_activity(self) -> tuple[ActivityFact, ...]:
        recent = sorted((*self._important.values(), *self._ordinary),
                        key=lambda item: item.sequence)
        return tuple(recent[-16:])

    @property
    def blockers(self) -> tuple[BlockerFact, ...]:
        return tuple(sorted(self._blockers.values(), key=lambda item: item.blocker_id))

    @property
    def committed_actions_since_goal_progress(self) -> int:
        return self._actions_since_goal_progress

    @property
    def goal_directed_actions_since_goal_progress(self) -> int:
        return self._goal_directed_actions_since_goal_progress

    @property
    def actions_since_artifact_change(self) -> int | None:
        return self._actions_since_artifact_change

    @property
    def verification_actions_since_artifact_change(self) -> int:
        return self._verification_actions_since_artifact_change

    @property
    def recovery_epochs(self) -> int:
        return self._recovery_epochs

    @property
    def repeated_branch_count(self) -> int:
        return self._repeated_branch_count

    @property
    def artifact_revision(self) -> int:
        return self._artifact_revision

    @property
    def verification_obligations(self) -> tuple[VerificationObligation, ...]:
        return tuple(sorted(self._verification_obligations.values(),
                            key=lambda item: item.criterion_id))

    @property
    def pending_verification_obligations(self) -> tuple[VerificationObligation, ...]:
        return tuple(item for item in self.verification_obligations
                     if item.status == "PENDING")

    @property
    def logical_revision(self) -> int:
        return self._logical_revision

    def ensure_criteria(self, goal_contract: Mapping[str, Any]) -> None:
        """Initialize CFW's criterion ledger independently of provider availability."""
        declared = list(goal_contract.get("acceptance_criteria") or ())
        if len(declared) > 256:
            raise ValueError("CFW goal contract exceeds the 256-criterion task-set bound")
        for item in declared:
            criterion_id = str(item.get("criterion_id") or "")[:128]
            if not criterion_id:
                continue
            current = self._criteria.get(criterion_id)
            if current is None:
                self._criteria[criterion_id] = CriterionState(
                    criterion_id=criterion_id,
                    claim=str(item.get("claim") or "")[:512],
                    kind=str(item.get("kind") or "")[:64])
                self._criterion_action_counts.setdefault(criterion_id, 0)
                self._criterion_retry_counts.setdefault(criterion_id, 0)
                self._criterion_redirect_counts.setdefault(criterion_id, 0)
                self._criterion_last_action_sequence.setdefault(criterion_id, 0)
                self._logical_revision += 1
            if current is None or criterion_id not in self._verification_requirements:
                self._verification_requirements[criterion_id] = (
                    str(item.get("verification_strength") or "host_observation"),
                    str(item.get("evidence_kind") or item.get("required_evidence_kind") or "") or None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "next_sequence": self._next_sequence,
            "ordinary": [item.to_dict() for item in self._ordinary],
            "important": [item.to_dict() for item in sorted(
                self._important.values(), key=lambda fact: fact.sequence)],
            "workflows": [item.to_dict() for item in self._workflows.values()],
            "failed_approaches": [item.to_dict() for item in self._failed_approaches],
            "criteria": [item.to_dict() for item in self.criteria],
            "blockers": {key: value.to_dict() for key, value in self._blockers.items()},
            "actions_since_goal_progress": self._actions_since_goal_progress,
            "goal_directed_actions_since_goal_progress": (
                self._goal_directed_actions_since_goal_progress),
            "actions_since_artifact_change": self._actions_since_artifact_change,
            "verification_actions_since_artifact_change": self._verification_actions_since_artifact_change,
            "artifact_revision": self._artifact_revision,
            "verification_counts": dict(self._verification_counts),
            "verification_fingerprints": dict(self._verification_fingerprints),
            "logical_revision": self._logical_revision,
            "recovery_epochs": self._recovery_epochs,
            "repeated_branch_count": self._repeated_branch_count,
            "verification_obligations": [item.to_dict()
                                         for item in self.verification_obligations],
            "criterion_attention": {
                "action_counts": dict(self._criterion_action_counts),
                "retry_counts": dict(self._criterion_retry_counts),
                "last_action_sequence": dict(self._criterion_last_action_sequence),
                "redirect_counts": dict(self._criterion_redirect_counts),
                "progress_redirect_epoch": self._progress_redirect_epoch,
                "last_progress_redirect_action": self._last_progress_redirect_action,
                "last_progress_redirect_criterion": self._last_progress_redirect_criterion,
                "progress_redirect_yield_target": self._progress_redirect_yield_target,
                "progress_redirect_guidance_pending": self._progress_redirect_guidance_pending,
            },
        }

    def restore(self, value: Mapping[str, Any]) -> None:
        fields = {
            "next_sequence", "ordinary", "important", "workflows", "failed_approaches",
            "criteria", "blockers", "actions_since_goal_progress",
            "actions_since_artifact_change", "verification_actions_since_artifact_change",
            "recovery_epochs", "repeated_branch_count",
        }
        extended_fields = fields | {
            "goal_directed_actions_since_goal_progress", "artifact_revision",
            "verification_counts", "verification_fingerprints", "logical_revision",
        }
        obligation_fields = extended_fields | {"verification_obligations"}
        attention_fields = extended_fields | {"criterion_attention"}
        current_fields = (extended_fields | {"verification_obligations", "criterion_attention"})
        if not isinstance(value, Mapping) or set(value) not in (
                fields, extended_fields, obligation_fields, attention_fields,
                current_fields):
            raise ValueError("malformed metacognitive state snapshot")

        def activity(row: Mapping[str, Any]) -> ActivityFact:
            keys = {"sequence", "activity_type", "source", "action_class",
                    "host_declared_relation", "target_kind", "target_scope",
                    "target_ref_digest", "proposed_purpose", "proposed_purpose_source",
                    "affected_criterion_ids", "result_class", "artifact_changed",
                    "progress_class", "evidence_kinds"}
            if (not isinstance(row, Mapping)
                    or set(row) not in (keys, keys | {"relation_certainty"})):
                raise ValueError("malformed System-1 activity snapshot")
            return ActivityFact(
                sequence=int(row["sequence"]), activity_type=str(row["activity_type"]),
                source=str(row["source"]), action_class=row["action_class"],
                host_declared_relation=row["host_declared_relation"],
                target_kind=row["target_kind"], target_scope=row["target_scope"],
                target_ref_digest=row["target_ref_digest"],
                proposed_purpose=row["proposed_purpose"],
                proposed_purpose_source=str(row["proposed_purpose_source"]),
                affected_criterion_ids=tuple(row["affected_criterion_ids"]),
                result_class=row["result_class"], artifact_changed=row["artifact_changed"],
                progress_class=row["progress_class"], evidence_kinds=tuple(row["evidence_kinds"]),
                relation_certainty=row.get("relation_certainty"))

        def criterion(row: Mapping[str, Any]) -> CriterionState:
            return CriterionState(
                criterion_id=str(row["criterion_id"]), claim=str(row["claim"]),
                kind=str(row["kind"]), status=str(row["status"]),
                evidence_status=str(row["evidence_status"]),
                evidence_ref_digests=tuple(row["evidence_ref_digests"]),
                last_progress_sequence=row["last_progress_sequence"])

        def workflow(row: Mapping[str, Any]) -> WorkflowProgressFact:
            payload = dict(row)
            if "workflow_revision" in payload:
                payload["evidence_refs"] = tuple(payload.get("evidence_refs") or ())
                return WorkflowProgressFact(**payload)
            # Migrate snapshots written before the engine-neutral projection.
            # Missing completion data stays incomplete; it never implies success.
            return WorkflowProgressFact(
                statework_id=str(payload["statework_id"]),
                subject_ref=str(payload.get("subject_ref") or "legacy-subject"),
                engine_kind=str(payload.get("engine_kind") or "state_machine"),
                workflow_revision=int(payload.get("revision") or 0),
                step_id=str(payload.get("step_id") or payload.get("edge_id") or "legacy-step"),
                work_class=str(payload.get("work_class") or "workflow"),
                domain_effect=str(payload.get("domain_effect") or "workflow"),
                progress_class=str(payload["progress_class"]),
                current_position=payload.get("current_state"),
                previous_position=payload.get("previous_state"),
                completed=payload.get("completed") is True,
                source=str(payload.get("source") or "csw_authoritative"))

        ordinary = [activity(row) for row in value["ordinary"]]
        important = [activity(row) for row in value["important"]]
        workflows = [workflow(row) for row in value["workflows"]]
        failed = [AttemptSemanticDescriptor(**{
            **dict(row), "affected_criterion_ids": tuple(row["affected_criterion_ids"])
        }) for row in value["failed_approaches"]]
        criteria = [criterion(row) for row in value["criteria"]]
        if (len(ordinary) > self._ordinary.maxlen
                or len(important) > self._important_limit
                or len(workflows) > self._workflow_limit
                or len(failed) > self._failed_approaches.maxlen):
            raise ValueError("System-1 projector snapshot exceeds configured bounds")
        next_sequence = int(value["next_sequence"])
        if next_sequence < max((item.sequence for item in ordinary + important), default=0):
            raise ValueError("System-1 projector sequence predates stored activity")
        self._ordinary.clear()
        self._ordinary.extend(ordinary)
        self._important = {}
        for item in important:
            key = "artifact_change" if item.artifact_changed is True else item.activity_type
            self._important[key] = item
        self._workflows = {
            (item.statework_id, item.subject_ref): item for item in workflows}
        self._failed_approaches.clear()
        self._failed_approaches.extend(failed)
        self._criteria = {item.criterion_id: item for item in criteria}
        attention = value.get("criterion_attention") or {}
        prior_attention_fields = {"action_counts", "retry_counts", "last_action_sequence"}
        redirect_attention_fields = prior_attention_fields | {
            "redirect_counts", "progress_redirect_epoch",
            "last_progress_redirect_action", "last_progress_redirect_criterion",
            "progress_redirect_yield_target", "progress_redirect_guidance_pending",
        }
        if not isinstance(attention, Mapping) or set(attention) not in (
                prior_attention_fields, redirect_attention_fields, set()):
            raise ValueError("malformed criterion attention snapshot")
        criterion_ids = set(self._criteria)
        restored_attention = {}
        for name in ("action_counts", "retry_counts", "last_action_sequence",
                     "redirect_counts"):
            source = attention.get(name) or {}
            if (not isinstance(source, Mapping) or len(source) > 256
                or not set(source).issubset(criterion_ids | {"__task__"})
                    or any(type(count) is not int or count < 0
                           for count in source.values())):
                raise ValueError("malformed criterion attention counters")
            restored_attention[name] = {str(key): int(count)
                                       for key, count in source.items()}
        self._criterion_action_counts = {
            key: restored_attention["action_counts"].get(key, 0) for key in criterion_ids}
        self._criterion_retry_counts = {
            key: restored_attention["retry_counts"].get(key, 0) for key in criterion_ids}
        self._criterion_last_action_sequence = {
            key: restored_attention["last_action_sequence"].get(key, 0)
            for key in criterion_ids}
        self._criterion_redirect_counts = {
            key: restored_attention["redirect_counts"].get(key, 0)
            for key in criterion_ids}
        if "__task__" in restored_attention["redirect_counts"]:
            self._criterion_redirect_counts["__task__"] = restored_attention[
                "redirect_counts"]["__task__"]
        self._progress_redirect_epoch = int(attention.get("progress_redirect_epoch", 0) or 0)
        if self._progress_redirect_epoch < 0:
            raise ValueError("malformed progress redirect epoch")
        self._last_progress_redirect_action = (
            str(attention["last_progress_redirect_action"])
            if attention.get("last_progress_redirect_action") else None)
        self._last_progress_redirect_criterion = (
            str(attention["last_progress_redirect_criterion"])
            if attention.get("last_progress_redirect_criterion") else None)
        yield_target = attention.get("progress_redirect_yield_target")
        if yield_target is not None and (
                not isinstance(yield_target, str) or yield_target not in criterion_ids):
            raise ValueError("malformed progress redirect yield target")
        self._progress_redirect_yield_target = yield_target
        self._progress_redirect_guidance_pending = (
            attention.get("progress_redirect_guidance_pending") is True)
        self._blockers = {}
        for key, row in value["blockers"].items():
            if isinstance(row, Mapping):
                blocker = BlockerFact(
                    blocker_id=str(row.get("blocker_id") or key),
                    summary=str(row.get("summary") or "")[:256],
                    criterion_ids=tuple(row.get("criterion_ids") or ())[:8],
                    evidence_ref=(str(row["evidence_ref"])
                                  if row.get("evidence_ref") else None))
            else:
                # Read older journals where blocker snapshots held criterion IDs only.
                blocker = BlockerFact(str(key), "", tuple(row)[:8])
            self._blockers[str(key)] = blocker
        self._next_sequence = next_sequence
        self._actions_since_goal_progress = int(value["actions_since_goal_progress"])
        self._goal_directed_actions_since_goal_progress = int(
            value.get("goal_directed_actions_since_goal_progress", 0))
        self._actions_since_artifact_change = value["actions_since_artifact_change"]
        self._verification_actions_since_artifact_change = int(
            value["verification_actions_since_artifact_change"])
        self._artifact_revision = int(value.get("artifact_revision", 0))
        self._verification_counts = {str(key): int(count) for key, count in
                                     value.get("verification_counts", {}).items()}
        self._verification_fingerprints = {
            str(key): int(count) for key, count in
            value.get("verification_fingerprints", {}).items()}
        self._logical_revision = int(value.get("logical_revision", next_sequence))
        self._recovery_epochs = int(value["recovery_epochs"])
        self._repeated_branch_count = int(value["repeated_branch_count"])
        self._verification_obligations = {}
        for raw in value.get("verification_obligations", ()):
            if not isinstance(raw, Mapping):
                raise ValueError(  # noqa: TRY004 - persisted snapshot structure is invalid
                    "malformed verification obligation snapshot")
            obligation = VerificationObligation(
                criterion_id=str(raw.get("criterion_id") or ""),
                artifact_revision=int(raw.get("artifact_revision", -1)),
                required_strength=str(raw.get("required_strength") or ""),
                evidence_kind=(str(raw["evidence_kind"])
                               if raw.get("evidence_kind") else None),
                status=str(raw.get("status") or "PENDING"))
            self._verification_obligations[obligation.criterion_id] = obligation

    def record_activity(self, activity_type: str, *, source: str = "host_observed",
                        action_class: str | None = None,
                        host_declared_relation: str | None = None,
                        target_kind: str | None = None,
                        target_scope: str | None = None,
                        target_ref: str | None = None,
                        proposed_purpose: str | None = None,
                        proposed_purpose_source: str = "derived",
                        affected_criterion_ids: Sequence[str] = (),
                        result_class: str | None = None,
                        artifact_changed: bool | None = None,
                        progress_class: str | None = None,
                        evidence_kinds: Sequence[str] = (),
                        verification_relation: str | None = None) -> ActivityFact:
        candidate = self.make_candidate(
            activity_type, source=source, action_class=action_class,
            host_declared_relation=host_declared_relation, target_kind=target_kind,
            target_scope=target_scope, target_ref=target_ref,
            proposed_purpose=proposed_purpose,
            proposed_purpose_source=proposed_purpose_source,
            affected_criterion_ids=affected_criterion_ids, result_class=result_class,
            artifact_changed=artifact_changed, progress_class=progress_class,
            evidence_kinds=evidence_kinds)
        return self.commit_candidate(candidate, verification_relation=verification_relation)

    def make_candidate(self, activity_type: str, *, source: str = "host_observed",
                       action_class: str | None = None,
                       host_declared_relation: str | None = None,
                       target_kind: str | None = None,
                       target_scope: str | None = None,
                       target_ref: str | None = None,
                       proposed_purpose: str | None = None,
                       proposed_purpose_source: str = "derived",
                       affected_criterion_ids: Sequence[str] = (),
                       result_class: str | None = None,
                       artifact_changed: bool | None = None,
                       progress_class: str | None = None,
                       evidence_kinds: Sequence[str] = ()) -> ActivityFact:
        """Build a candidate fact without changing history or any progress counter."""
        target_digest = (hashlib.sha256(target_ref.encode("utf-8")).hexdigest()
                         if target_ref else None)
        return ActivityFact(
            sequence=self._next_sequence + 1, activity_type=activity_type, source=source,
            action_class=action_class, host_declared_relation=host_declared_relation,
            target_kind=target_kind, target_scope=target_scope,
            target_ref_digest=target_digest, proposed_purpose=proposed_purpose,
            proposed_purpose_source=proposed_purpose_source,
            affected_criterion_ids=tuple(affected_criterion_ids[:8]),
            result_class=result_class, artifact_changed=artifact_changed,
            progress_class=progress_class, evidence_kinds=tuple(evidence_kinds[:8]))
    def commit_candidate(self, candidate: ActivityFact, *,
                         verification_relation: str | None = None) -> ActivityFact:
        """Commit only a fact whose action/result has crossed its host boundary."""
        if not isinstance(candidate, ActivityFact):
            raise TypeError("metacognitive history accepts typed activity facts only")
        self._next_sequence += 1
        self._logical_revision += 1
        fact = replace(candidate, sequence=self._next_sequence)
        activity_type = fact.activity_type
        artifact_changed = fact.artifact_changed
        if activity_type in self._IMPORTANT or artifact_changed is True:
            key = "artifact_change" if artifact_changed is True else activity_type
            self._important[key] = fact
            while len(self._important) > self._important_limit:
                oldest = min(self._important, key=lambda item: self._important[item].sequence)
                del self._important[oldest]
        else:
            self._ordinary.append(fact)
        if activity_type == "action":
            self._actions_since_goal_progress += 1
            if fact.host_declared_relation in {
                    "advances_primary_goal", "removes_concrete_blocker"}:
                self._goal_directed_actions_since_goal_progress += 1
            if self._actions_since_artifact_change is not None:
                self._actions_since_artifact_change += 1
                if verification_relation == "required_verification":
                    self._verification_actions_since_artifact_change += 1
        if activity_type in {"action", "retry"}:
            for criterion_id in fact.affected_criterion_ids:
                if criterion_id not in self._criteria:
                    continue
                self._criterion_action_counts[criterion_id] = (
                    self._criterion_action_counts.get(criterion_id, 0) + 1)
                self._criterion_last_action_sequence[criterion_id] = fact.sequence
        if artifact_changed is True:
            self._artifact_revision += 1
            self._actions_since_artifact_change = 0
            self._verification_actions_since_artifact_change = 0
            self._verification_counts.clear()
            self._verification_fingerprints.clear()
            for criterion_id in fact.affected_criterion_ids:
                requirement = self._verification_requirements.get(
                    criterion_id, ("host_observation", None))
                self._verification_obligations[criterion_id] = VerificationObligation(
                    criterion_id=criterion_id, artifact_revision=self._artifact_revision,
                    required_strength=requirement[0], evidence_kind=requirement[1])
                prior = self._criteria.get(criterion_id)
                if prior is not None:
                    self._criteria[criterion_id] = CriterionState(
                        criterion_id=prior.criterion_id, claim=prior.claim, kind=prior.kind,
                        status="active", evidence_status="invalidated",
                        evidence_ref_digests=(),
                        last_progress_sequence=prior.last_progress_sequence)
        if activity_type == "goal_progress" and fact.progress_class in {
                "GOAL_PROGRESS", "BLOCKER_REMOVED"}:
            self._actions_since_goal_progress = 0
            self._goal_directed_actions_since_goal_progress = 0
            for criterion_id in fact.affected_criterion_ids:
                if criterion_id in self._criteria:
                    self._criterion_retry_counts[criterion_id] = 0
            self._reset_progress_redirects()
        elif artifact_changed is True:
            self._reset_progress_redirects()
        if activity_type == "retry" and fact.result_class == "equivalent_retry":
            self._repeated_branch_count += 1
        if (activity_type == "action"
                and verification_relation == "required_verification"
                and artifact_changed is not True):
            signature = hashlib.sha256(json.dumps({
                "action_class": fact.action_class,
                "target_ref_digest": fact.target_ref_digest,
                "criterion_ids": list(fact.affected_criterion_ids),
                "artifact_revision": self._artifact_revision,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            scope = f"{self._artifact_revision}:{','.join(fact.affected_criterion_ids)}"
            self._verification_counts[scope] = self._verification_counts.get(scope, 0) + 1
            self._verification_fingerprints[signature] = (
                self._verification_fingerprints.get(signature, 0) + 1)
        return fact

    def verification_limit_exceeded(self, candidate: ActivityFact, *,
                                    relation: str, max_actions: int,
                                    max_repeats: int) -> str | None:
        """Reject repeated verification of unchanged evidence targets."""
        if relation != "required_verification":
            return None
        signature = hashlib.sha256(json.dumps({
            "action_class": candidate.action_class,
            "target_ref_digest": candidate.target_ref_digest,
            "criterion_ids": list(candidate.affected_criterion_ids),
            "artifact_revision": self._artifact_revision,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        scope = f"{self._artifact_revision}:{','.join(candidate.affected_criterion_ids)}"
        if self._verification_counts.get(scope, 0) >= max_actions:
            return "verification_budget_exhausted"
        if self._verification_fingerprints.get(signature, 0) > max_repeats:
            return "equivalent_verification_repeated"
        return None

    def has_recent_artifact_change_for(self, criterion_id: str, *,
                                       before_sequence: int, max_age: int = 8) -> bool:
        """Correlate a later observation with a recent host-observed artifact change."""
        if not criterion_id or not isinstance(before_sequence, int) or max_age < 1:
            return False
        fact = self._important.get("artifact_change")
        if fact is None or fact.sequence >= before_sequence or fact.artifact_changed is not True:
            return False
        if criterion_id not in fact.affected_criterion_ids:
            return False
        return before_sequence - fact.sequence <= max_age

    def record_goal_progress(self, binding: CriterionEvidenceBinding, *,
                             claim: str, kind: str) -> None:
        if not isinstance(binding, CriterionEvidenceBinding):
            raise TypeError("hard criterion progress requires a host evidence binding")
        digest = hashlib.sha256(binding.evidence_ref.encode("utf-8")).hexdigest()
        prior = self._criteria.get(binding.criterion_id)
        if prior is None:
            raise ValueError("hard progress binding references an unknown criterion")
        refs = list(prior.evidence_ref_digests if prior else ())
        if digest not in refs:
            refs.append(digest)
        refs = refs[-8:]
        self._criteria[binding.criterion_id] = CriterionState(
            criterion_id=binding.criterion_id, claim=claim, kind=kind,
            status="partially_observed", evidence_status="available",
            evidence_ref_digests=tuple(refs),
            last_progress_sequence=self._next_sequence + 1)
        self.record_activity(
            "goal_progress", source=binding.authority,
            affected_criterion_ids=(binding.criterion_id,),
            progress_class="GOAL_PROGRESS", evidence_kinds=(binding.evidence_kind,))

    def mark_criterion_satisfied(self, criterion_id: str, *, evidence_ref: str,
                                 evidence_kind: str) -> None:
        prior = self._criteria.get(criterion_id)
        if prior is None:
            raise ValueError("cannot satisfy an unknown criterion")
        digest = hashlib.sha256(evidence_ref.encode("utf-8")).hexdigest()
        refs = tuple((list(prior.evidence_ref_digests) + [digest])[-8:])
        self._criteria[criterion_id] = CriterionState(
            criterion_id=prior.criterion_id, claim=prior.claim, kind=prior.kind,
            status="satisfied", evidence_status="verified", evidence_ref_digests=refs,
            last_progress_sequence=prior.last_progress_sequence)
        obligation = self._verification_obligations.get(criterion_id)
        if obligation is not None and obligation.status == "PENDING":
            self._verification_obligations[criterion_id] = replace(
                obligation, status="SATISFIED")
        self._logical_revision += 1

    def record_blocker(self, blocker_id: str, summary: str,
                       criterion_ids: Sequence[str] = (), *,
                       evidence_ref: str | None = None) -> None:
        if not blocker_id or not summary:
            raise ValueError("blockers require a bounded host identifier and summary")
        key = blocker_id[:128]
        self._blockers[key] = BlockerFact(
            blocker_id=key, summary=summary[:256],
            criterion_ids=tuple(criterion_ids[:8]),
            evidence_ref=evidence_ref[:256] if evidence_ref else None)
        self._logical_revision += 1
        self.record_activity("blocker", source="host_observed", result_class="active",
                             proposed_purpose=summary[:256],
                             proposed_purpose_source="host_observed",
                             affected_criterion_ids=tuple(criterion_ids[:8]))

    def clear_blocker(self, blocker_id: str) -> None:
        if self._blockers.pop(blocker_id, None) is not None:
            self._logical_revision += 1

    def record_workflow(self, fact: WorkflowProgressFact) -> None:
        key = (fact.statework_id, fact.subject_ref or "")
        self._workflows[key] = fact
        self._logical_revision += 1
        while len(self._workflows) > self._workflow_limit:
            oldest = next(iter(self._workflows))
            del self._workflows[oldest]

    def record_workflow_progress(self, *, statework_id: str, subject_ref: str,
                                 engine_kind: str, workflow_revision: int,
                                 step_id: str, progress_class: str,
                                 completed: bool, work_class: str,
                                 domain_effect: str,
                                 current_position: str | None = None,
                                 previous_position: str | None = None,
                                 evidence_refs: Sequence[str] = ()) -> None:
        self.record_workflow(WorkflowProgressFact(
            statework_id=statework_id, subject_ref=subject_ref,
            engine_kind=engine_kind, workflow_revision=int(workflow_revision),
            step_id=step_id, progress_class=progress_class,
            completed=bool(completed), work_class=work_class,
            domain_effect=domain_effect, current_position=current_position,
            previous_position=previous_position,
            evidence_refs=tuple(str(item) for item in evidence_refs)))
        self.record_activity("workflow_progress", source="csw_authoritative",
                             action_class=work_class,
                             result_class=progress_class,
                             progress_class="WORKFLOW_PROGRESS")

    def record_failed_approach(self, descriptor: AttemptSemanticDescriptor) -> None:
        self._failed_approaches.append(descriptor)
        for criterion_id in descriptor.affected_criterion_ids:
            if criterion_id in self._criteria:
                self._criterion_retry_counts[criterion_id] = (
                    self._criterion_retry_counts.get(criterion_id, 0) + 1)
                self._criterion_last_action_sequence[criterion_id] = self._next_sequence
        self._logical_revision += 1

    def record_recovery_epoch(self) -> None:
        """Advance the host revision when recovery state changes without an action."""
        self._recovery_epochs += 1
        self._logical_revision += 1


__all__ = [
    "BlockerFact",
    "CompletionReadiness",
    "CriterionEvidenceBinding",
    "MetaDecision",
    "MetacognitiveController",
    "MetacognitiveState",
    "SemanticSignalRecord",
    "SemanticSignalStore",
    "SemanticSignals",
    "VerificationObligation",
]
