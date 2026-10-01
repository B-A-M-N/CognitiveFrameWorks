"""Provider-independent, host-owned metacognitive working state."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Mapping, Optional, Sequence

from .system_one import (ActivityFact, AttemptSemanticDescriptor, CriterionState,
                         WorkflowFact)


@dataclass(frozen=True)
class BlockerFact:
    blocker_id: str
    summary: str
    criterion_ids: tuple[str, ...] = ()
    evidence_ref: Optional[str] = None

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
    artifact_revision: Optional[str] = None

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

    action_relation: Optional[str] = None
    progress_relation: Optional[str] = None
    equivalent_retry: Optional[bool] = None
    possible_goal_drift: Optional[bool] = None
    route_preference: Optional[str] = None
    source_decisions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"action_relation": self.action_relation,
                "progress_relation": self.progress_relation,
                "equivalent_retry": self.equivalent_retry,
                "possible_goal_drift": self.possible_goal_drift,
                "route_preference": self.route_preference,
                "source_decisions": list(self.source_decisions)}


@dataclass(frozen=True)
class MetaDecision:
    mode: str
    reason: str
    active_criterion_ids: tuple[str, ...] = ()
    goal_reanchor_required: bool = False
    semantic_attention_note: Optional[str] = None
    source_decisions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"DELIVER", "DIAGNOSE", "VERIFY", "RECOVER", "REPLAN",
                             "BLOCKED", "REINSPECT"}:
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

    def evaluate(self, state: "MetacognitiveState", *,
                 support_actions_since_progress: int,
                 support_action_limit: int,
                 recovery_epochs_since_progress: int,
                 max_recovery_epochs: int,
                 same_approach_retries: int,
                 max_same_approach_retries: int,
                 unresolved_contradiction: bool = False,
                 semantic: Optional[SemanticSignals] = None) -> MetaDecision:
        active = tuple(item.criterion_id for item in state.criteria
                       if item.status != "satisfied")[:8]
        if unresolved_contradiction:
            return MetaDecision("REINSPECT", "unresolved operator contradiction", active)
        if recovery_epochs_since_progress >= max_recovery_epochs:
            return MetaDecision("BLOCKED", "hard recovery budget exhausted", active)
        if support_actions_since_progress >= support_action_limit:
            return MetaDecision("REPLAN", "support-action budget reached", active,
                                goal_reanchor_required=True)
        if same_approach_retries >= max_same_approach_retries:
            return MetaDecision("RECOVER", "repeated approach reached its hard limit", active)
        if state.blockers:
            return MetaDecision("DIAGNOSE", "host-observed blocker remains", active)
        if (state.actions_since_artifact_change == 0 and active
                and state.verification_actions_since_artifact_change == 0):
            return MetaDecision("VERIFY", "changed artifact needs first criterion check", active)
        note = None
        sources: tuple[str, ...] = ()
        if semantic is not None:
            sources = semantic.source_decisions[:8]
            if semantic.possible_goal_drift:
                note = "A semantic signal suggests possible drift; keep the next action tied to an unfinished criterion."
        if active:
            return MetaDecision("DELIVER", "required criterion remains unfinished", active,
                                semantic_attention_note=note, source_decisions=sources)
        return MetaDecision("VERIFY", "confirm required criteria and completion evidence",
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
        self._workflows: dict[str, WorkflowFact] = {}
        self._failed_approaches = deque(maxlen=failed_approach_limit)
        self._important_limit = important_limit
        self._workflow_limit = workflow_limit
        self._next_sequence = 0
        self._criteria: dict[str, CriterionState] = {}
        self._blockers: dict[str, BlockerFact] = {}
        self._actions_since_goal_progress = 0
        self._actions_since_artifact_change: Optional[int] = None
        self._verification_actions_since_artifact_change = 0
        self._artifact_revision = 0
        self._verification_counts: dict[str, int] = {}
        self._verification_fingerprints: dict[str, int] = {}
        self._goal_directed_actions_since_goal_progress = 0
        self._recovery_epochs = 0
        self._repeated_branch_count = 0
        self._logical_revision = 0

    @property
    def criteria(self) -> tuple[CriterionState, ...]:
        return tuple(sorted(self._criteria.values(), key=lambda item: item.criterion_id))

    @property
    def workflows(self) -> tuple[WorkflowFact, ...]:
        return tuple(self._workflows.values())

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
    def actions_since_artifact_change(self) -> Optional[int]:
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
    def logical_revision(self) -> int:
        return self._logical_revision

    def ensure_criteria(self, goal_contract: Mapping[str, Any]) -> None:
        """Initialize CFW's criterion ledger independently of provider availability."""
        for item in list(goal_contract.get("acceptance_criteria") or ())[:8]:
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
        if not isinstance(value, Mapping) or set(value) not in (fields, extended_fields):
            raise ValueError("malformed metacognitive state snapshot")

        def activity(row: Mapping[str, Any]) -> ActivityFact:
            keys = {"sequence", "activity_type", "source", "action_class",
                    "host_declared_relation", "target_kind", "target_scope",
                    "target_ref_digest", "proposed_purpose", "proposed_purpose_source",
                    "affected_criterion_ids", "result_class", "artifact_changed",
                    "progress_class", "evidence_kinds"}
            if not isinstance(row, Mapping) or set(row) != keys:
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
                progress_class=row["progress_class"], evidence_kinds=tuple(row["evidence_kinds"]))

        def criterion(row: Mapping[str, Any]) -> CriterionState:
            return CriterionState(
                criterion_id=str(row["criterion_id"]), claim=str(row["claim"]),
                kind=str(row["kind"]), status=str(row["status"]),
                evidence_status=str(row["evidence_status"]),
                evidence_ref_digests=tuple(row["evidence_ref_digests"]),
                last_progress_sequence=row["last_progress_sequence"])

        def workflow(row: Mapping[str, Any]) -> WorkflowFact:
            return WorkflowFact(**dict(row))

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
        self._workflows = {item.statework_id: item for item in workflows}
        self._failed_approaches.clear()
        self._failed_approaches.extend(failed)
        self._criteria = {item.criterion_id: item for item in criteria}
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

    def record_activity(self, activity_type: str, *, source: str = "host_observed",
                        action_class: Optional[str] = None,
                        host_declared_relation: Optional[str] = None,
                        target_kind: Optional[str] = None,
                        target_scope: Optional[str] = None,
                        target_ref: Optional[str] = None,
                        proposed_purpose: Optional[str] = None,
                        proposed_purpose_source: str = "derived",
                        affected_criterion_ids: Sequence[str] = (),
                        result_class: Optional[str] = None,
                        artifact_changed: Optional[bool] = None,
                        progress_class: Optional[str] = None,
                        evidence_kinds: Sequence[str] = (),
                        verification_relation: Optional[str] = None) -> ActivityFact:
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
                       action_class: Optional[str] = None,
                       host_declared_relation: Optional[str] = None,
                       target_kind: Optional[str] = None,
                       target_scope: Optional[str] = None,
                       target_ref: Optional[str] = None,
                       proposed_purpose: Optional[str] = None,
                       proposed_purpose_source: str = "derived",
                       affected_criterion_ids: Sequence[str] = (),
                       result_class: Optional[str] = None,
                       artifact_changed: Optional[bool] = None,
                       progress_class: Optional[str] = None,
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
                         verification_relation: Optional[str] = None) -> ActivityFact:
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
        if artifact_changed is True:
            self._artifact_revision += 1
            self._actions_since_artifact_change = 0
            self._verification_actions_since_artifact_change = 0
            self._verification_counts.clear()
            self._verification_fingerprints.clear()
        if activity_type == "goal_progress" and fact.progress_class in {
                "GOAL_PROGRESS", "BLOCKER_REMOVED"}:
            self._actions_since_goal_progress = 0
            self._goal_directed_actions_since_goal_progress = 0
        if activity_type == "retry" and result_class == "equivalent_retry":
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
                                    max_repeats: int) -> Optional[str]:
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
        self._logical_revision += 1

    def record_blocker(self, blocker_id: str, summary: str,
                       criterion_ids: Sequence[str] = (), *,
                       evidence_ref: Optional[str] = None) -> None:
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

    def record_workflow(self, fact: WorkflowFact) -> None:
        self._workflows[fact.statework_id] = fact
        self._logical_revision += 1
        while len(self._workflows) > self._workflow_limit:
            oldest = next(iter(self._workflows))
            del self._workflows[oldest]

    def record_csw_fact(self, *, statework_id: str, input_state: str,
                        output_state: str, progress_class: str,
                        workflow_metadata: Optional[Mapping[str, Any]] = None) -> None:
        metadata = dict(workflow_metadata or {})
        self.record_workflow(WorkflowFact(
            statework_id=statework_id, current_state=output_state,
            previous_state=input_state, progress_class=progress_class,
            edge_id=str(metadata.get("edge_id") or "") or None,
            trigger=str(metadata.get("trigger") or "") or None,
            path_class=str(metadata.get("path_class") or "") or None,
            work_class=str(metadata.get("work_class") or "") or None,
            domain_effect=str(metadata.get("domain_effect") or "") or None,
            revision=(int(metadata["revision"]) if metadata.get("revision") is not None else None),
            normal_path_rank_before=(int(metadata["normal_path_rank_before"])
                                     if metadata.get("normal_path_rank_before") is not None else None),
            normal_path_rank_after=(int(metadata["normal_path_rank_after"])
                                    if metadata.get("normal_path_rank_after") is not None else None),
            domain=str(metadata.get("domain") or "") or None,
            purpose=str(metadata.get("purpose") or "") or None))
        self.record_activity("state_transition", source="csw_authoritative",
                             action_class=str(metadata.get("work_class") or "transition"),
                             result_class=progress_class,
                             progress_class=("RECOVERY_PROGRESS" if progress_class in {
                                 "RECOVERY", "INVALIDATION", "REGRESSION"} else None))
        if progress_class in {"RECOVERY", "INVALIDATION", "REGRESSION"}:
            self._recovery_epochs += 1

    def record_failed_approach(self, descriptor: AttemptSemanticDescriptor) -> None:
        self._failed_approaches.append(descriptor)
        self._logical_revision += 1


__all__ = ["MetacognitiveState", "BlockerFact", "CriterionEvidenceBinding",
           "SemanticSignals", "MetaDecision", "MetacognitiveController"]
