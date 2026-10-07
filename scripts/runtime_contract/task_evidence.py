"""CFW-local criterion, evidence, and workflow fact ledger."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .behavior_facts import (
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


class TaskEvidenceState:
    """Keep bounded host facts for CFW's local gates and standalone fallback.

    This ledger records evidence and task observations. It does not select
    global behavior, own attention policy, or interpret semantic providers.
    """

    _IMPORTANT = frozenset({"goal_progress", "blocker", "retry", "validation",
                            "operator_correction"})

    def __init__(self, *, activity_limit: int = 8, important_limit: int = 8,
                 workflow_limit: int = 8, failed_approach_limit: int = 4) -> None:
        if not (1 <= activity_limit <= 8 and 1 <= important_limit <= 8
                and 1 <= workflow_limit <= 8 and 1 <= failed_approach_limit <= 4):
            raise ValueError("host fact projection limits exceed the safe bound")
        self._ordinary = deque(maxlen=activity_limit)
        self._important: dict[str, ActivityFact] = {}
        self._workflows: dict[tuple[str, str], WorkflowProgressFact] = {}
        self._failed_approaches = deque(maxlen=failed_approach_limit)
        self._important_limit = important_limit
        self._workflow_limit = workflow_limit
        self._next_sequence = 0
        self._criteria: dict[str, CriterionState] = {}
        self._blockers: dict[str, BlockerFact] = {}
        self._actions_since_artifact_change: int | None = None
        self._verification_actions_since_artifact_change = 0
        self._artifact_revision = 0
        self._verification_counts: dict[str, int] = {}
        self._verification_fingerprints: dict[str, int] = {}
        self._logical_revision = 0
        self._verification_requirements: dict[str, tuple[str, str | None]] = {}
        self._verification_obligations: dict[str, VerificationObligation] = {}

    @property
    def criteria(self) -> tuple[CriterionState, ...]:
        return tuple(sorted(self._criteria.values(), key=lambda item: item.criterion_id))

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
    def actions_since_artifact_change(self) -> int | None:
        return self._actions_since_artifact_change

    @property
    def verification_actions_since_artifact_change(self) -> int:
        return self._verification_actions_since_artifact_change

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

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def mark_host_state_transition(self) -> None:
        """Advance the local snapshot revision for host state stored elsewhere."""
        self._logical_revision += 1

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
            "actions_since_artifact_change": self._actions_since_artifact_change,
            "verification_actions_since_artifact_change": self._verification_actions_since_artifact_change,
            "artifact_revision": self._artifact_revision,
            "verification_counts": dict(self._verification_counts),
            "verification_fingerprints": dict(self._verification_fingerprints),
            "logical_revision": self._logical_revision,
            "verification_obligations": [item.to_dict()
                                         for item in self.verification_obligations],
        }

    def restore(self, value: Mapping[str, Any]) -> None:
        fields = {
            "next_sequence", "ordinary", "important", "workflows", "failed_approaches",
            "criteria", "blockers",
            "actions_since_artifact_change", "verification_actions_since_artifact_change",
        }
        legacy_global_fields = {
            "actions_since_goal_progress", "goal_directed_actions_since_goal_progress",
            "recovery_epochs", "repeated_branch_count",
        }
        extensions = {
            "artifact_revision",
            "verification_counts", "verification_fingerprints", "logical_revision",
        }
        attachments = (
            set(), {"verification_obligations"}, {"criterion_attention"},
            {"verification_obligations", "criterion_attention"},
        )
        legacy_counters = {"actions_since_goal_progress", "recovery_epochs",
                           "repeated_branch_count"}
        legacy_variants = {
            frozenset(fields | legacy_counters | attachment)
            for attachment in attachments
        } | {
            frozenset(fields | legacy_global_fields | extensions | attachment)
            for attachment in attachments
        }
        current_variants = {
            frozenset(fields | attachment) for attachment in attachments
        } | {
            frozenset(fields | extensions | attachment) for attachment in attachments
        }
        accepted_fields = legacy_variants | current_variants
        if (not isinstance(value, Mapping)
                or frozenset(value) not in accepted_fields):
            raise ValueError("malformed task evidence state snapshot")
        for name in legacy_global_fields & set(value):
            counter = value[name]
            if (not isinstance(counter, int) or isinstance(counter, bool)
                    or counter < 0):
                raise ValueError("malformed legacy global metacognitive counter")

        def activity(row: Mapping[str, Any]) -> ActivityFact:
            keys = {"sequence", "activity_type", "source", "action_class",
                    "host_declared_relation", "target_kind", "target_scope",
                    "target_ref_digest", "proposed_purpose", "proposed_purpose_source",
                    "affected_criterion_ids", "result_class", "artifact_changed",
                    "progress_class", "evidence_kinds"}
            if (not isinstance(row, Mapping)
                    or set(row) not in (keys, keys | {"relation_certainty"})):
                raise ValueError("malformed host activity snapshot")
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
            raise ValueError("host fact snapshot exceeds configured bounds")
        next_sequence = int(value["next_sequence"])
        if next_sequence < max((item.sequence for item in ordinary + important), default=0):
            raise ValueError("host fact sequence predates stored activity")
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
        # Attention payloads from older journals are validated above but are
        # restored only by StandaloneAttentionState under standalone authority.
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
            if self._actions_since_artifact_change is not None:
                self._actions_since_artifact_change += 1
                if verification_relation == "required_verification":
                    self._verification_actions_since_artifact_change += 1
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
                             claim: str, kind: str) -> ActivityFact:
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
        return self.record_activity(
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
        self._logical_revision += 1

__all__ = [
    "BlockerFact",
    "CompletionReadiness",
    "CriterionEvidenceBinding",
    "TaskEvidenceState",
    "VerificationObligation",
]
