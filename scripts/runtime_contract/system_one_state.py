"""Bounded semantic projection built only from host-owned facts and labels."""
from __future__ import annotations

from collections import deque
import hashlib
from typing import Any, Mapping, Optional, Sequence
import uuid

from .protocol_artifact import QUESTION_REGISTRY
from .system_one import (ActivityFact, AttemptSemanticDescriptor, CriterionState,
                         QuestionSpec, SemanticState, SystemOneFrame,
                         SystemOneIdentity, WorkflowFact, canonical_hash)
from .task import SystemOnePolicy


class SystemOneStateProjector:
    """Keeps semantically important facts plus a small recent ordinary tail."""

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
        self._blockers: dict[str, tuple[str, ...]] = {}
        self._actions_since_goal_progress = 0
        self._actions_since_artifact_change: Optional[int] = None
        self._verification_actions_since_artifact_change = 0
        self._recovery_epochs = 0
        self._repeated_branch_count = 0

    @property
    def criteria(self) -> tuple[CriterionState, ...]:
        return tuple(sorted(self._criteria.values(), key=lambda item: item.criterion_id))

    def snapshot(self) -> dict[str, Any]:
        return {
            "next_sequence": self._next_sequence,
            "ordinary": [item.to_dict() for item in self._ordinary],
            "important": [item.to_dict() for item in sorted(
                self._important.values(), key=lambda fact: fact.sequence)],
            "workflows": [item.to_dict() for item in self._workflows.values()],
            "failed_approaches": [item.to_dict() for item in self._failed_approaches],
            "criteria": [item.to_dict() for item in self.criteria],
            "blockers": {key: list(value) for key, value in self._blockers.items()},
            "actions_since_goal_progress": self._actions_since_goal_progress,
            "actions_since_artifact_change": self._actions_since_artifact_change,
            "verification_actions_since_artifact_change": self._verification_actions_since_artifact_change,
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
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("malformed System-1 projector snapshot")

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
        self._blockers = {str(key): tuple(rows) for key, rows in value["blockers"].items()}
        self._next_sequence = next_sequence
        self._actions_since_goal_progress = int(value["actions_since_goal_progress"])
        self._actions_since_artifact_change = value["actions_since_artifact_change"]
        self._verification_actions_since_artifact_change = int(
            value["verification_actions_since_artifact_change"])
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
                        evidence_kinds: Sequence[str] = ()) -> ActivityFact:
        self._next_sequence += 1
        target_digest = (hashlib.sha256(target_ref.encode("utf-8")).hexdigest()
                         if target_ref else None)
        fact = ActivityFact(
            sequence=self._next_sequence, activity_type=activity_type, source=source,
            action_class=action_class, host_declared_relation=host_declared_relation,
            target_kind=target_kind, target_scope=target_scope,
            target_ref_digest=target_digest, proposed_purpose=proposed_purpose,
            proposed_purpose_source=proposed_purpose_source,
            affected_criterion_ids=tuple(affected_criterion_ids[:8]),
            result_class=result_class, artifact_changed=artifact_changed,
            progress_class=progress_class, evidence_kinds=tuple(evidence_kinds[:8]))
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
            if self._actions_since_artifact_change is not None:
                self._actions_since_artifact_change += 1
                if action_class in {"verify", "render", "inspect"}:
                    self._verification_actions_since_artifact_change += 1
        if artifact_changed is True:
            self._actions_since_artifact_change = 0
            self._verification_actions_since_artifact_change = 0
        if progress_class in {"GOAL_PROGRESS", "BLOCKER_REMOVED"}:
            self._actions_since_goal_progress = 0
        if activity_type == "retry" and result_class == "equivalent_retry":
            self._repeated_branch_count += 1
        return fact

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

    def record_goal_progress(self, *, criterion_id: str, claim: str, kind: str,
                             evidence_ref: str, evidence_kind: str) -> None:
        if not criterion_id:
            raise ValueError("criterion_id is required to record criterion progress")
        digest = hashlib.sha256(evidence_ref.encode("utf-8")).hexdigest()
        prior = self._criteria.get(criterion_id)
        refs = list(prior.evidence_ref_digests if prior else ())
        if digest not in refs:
            refs.append(digest)
        refs = refs[-8:]
        self._criteria[criterion_id] = CriterionState(
            criterion_id=criterion_id, claim=claim, kind=kind,
            status="partially_observed", evidence_status="available",
            evidence_ref_digests=tuple(refs),
            last_progress_sequence=self._next_sequence + 1)
        self.record_activity(
            "goal_progress", source="host_observed", affected_criterion_ids=(criterion_id,),
            progress_class="GOAL_PROGRESS", evidence_kinds=(evidence_kind,))

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

    def record_blocker(self, blocker_id: str, summary: str,
                       criterion_ids: Sequence[str] = ()) -> None:
        if not blocker_id or not summary:
            raise ValueError("blockers require a bounded host identifier and summary")
        self._blockers[blocker_id[:128]] = tuple(criterion_ids[:8])
        self.record_activity("blocker", source="host_observed", result_class="active",
                             proposed_purpose=summary[:256],
                             proposed_purpose_source="host_observed",
                             affected_criterion_ids=tuple(criterion_ids[:8]))

    def clear_blocker(self, blocker_id: str) -> None:
        self._blockers.pop(blocker_id, None)

    def record_workflow(self, fact: WorkflowFact) -> None:
        self._workflows[fact.statework_id] = fact
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

    @staticmethod
    def _question_specs(question_ids: tuple[str, ...],
                        route_candidates: tuple[str, ...]) -> tuple[QuestionSpec, ...]:
        result = []
        for question_id in question_ids:
            source = QUESTION_REGISTRY.get(question_id)
            if source is None:
                raise ValueError("unknown System-1 question")
            criteria = source["criteria"]
            if question_id == "route_preference":
                if not route_candidates:
                    raise ValueError("route preference requires legal route candidates")
                criteria = {name: "Already-authorized legal route." for name in route_candidates}
            result.append(QuestionSpec(
                question_id=question_id, family=str(source["family"]),
                question_type=str(source["type"]), instructions=str(source["instructions"]),
                criteria=tuple((str(key), str(value)) for key, value in criteria.items())))
        return tuple(result)

    def project(self, *, identity: SystemOneIdentity, sequence: int,
                policy_hash: str, policy: SystemOnePolicy,
                questions: tuple[str, ...], goal_contract: Mapping[str, Any],
                convergence: Mapping[str, int], candidate: ActivityFact,
                phase: Optional[str] = None, active_segment: str = "segment-0",
                active_statework: Optional[str] = None, active_flow: Optional[str] = None,
                current_state: Optional[str] = None, blockers: Sequence[str] = (),
                failed_approaches: Sequence[AttemptSemanticDescriptor] = (),
                legal_routes: tuple[str, ...] = ()) -> SystemOneFrame:
        task = {
            "objective": str(goal_contract.get("objective") or "")[:1024],
            "work_mode": str(goal_contract.get("work_mode") or "delivery"),
            "deliverable_kind": str(goal_contract.get("deliverable_kind") or "code_behavior"),
            "task_complexity": str(goal_contract.get("task_complexity") or "standard"),
            "phase": (phase[:64] if phase else None),
            "active_segment": active_segment[:128] or "segment-0",
            "active_statework": active_statework[:64] if active_statework else None,
            "active_flow": active_flow[:64] if active_flow else None,
            "non_goals": tuple(str(item)[:256] for item in
                                list(goal_contract.get("non_goals") or ())[:8]),
        }
        criteria = []
        for item in list(goal_contract.get("acceptance_criteria") or ())[:8]:
            criterion_id = str(item.get("criterion_id") or "")[:128]
            prior = self._criteria.get(criterion_id)
            if prior is None:
                state = CriterionState(
                    criterion_id=criterion_id, claim=str(item.get("claim") or "")[:512],
                    kind=str(item.get("kind") or "")[:64])
                self._criteria[criterion_id] = state
            else:
                state = prior
            criteria.append(state)
        workflows = tuple(sorted(self._workflows.values(), key=lambda item: item.statework_id))
        recent = sorted((*self._important.values(), *self._ordinary), key=lambda item: item.sequence)
        activity = tuple(recent[-16:])
        convergence_values = {key: max(0, int(value)) for key, value in convergence.items()}
        situation = {
            "current_state": current_state[:128] if current_state else None,
            "blockers": tuple(str(item)[:256] for item in list(blockers)[:8]),
            "actions_since_goal_progress": max(
                self._actions_since_goal_progress,
                int(convergence_values.get("support_actions_since_goal_progress", 0))),
            "actions_since_artifact_change": self._actions_since_artifact_change,
            "verification_actions_since_artifact_change": self._verification_actions_since_artifact_change,
            "recovery_epochs": max(self._recovery_epochs,
                                    int(convergence_values.get("recovery_epochs_since_goal_progress", 0))),
            "repeated_branch_count": max(self._repeated_branch_count,
                                          int(convergence_values.get("same_approach_retries", 0))),
        }
        state = SemanticState(
            task=task, situation=situation, criteria=tuple(criteria), activity=activity,
            workflows=workflows, convergence=tuple(sorted(convergence_values.items())[:8]),
            candidate=candidate,
            failed_approaches=tuple(failed_approaches[-4:] or tuple(self._failed_approaches)),
            legal_routes=tuple(legal_routes[:32]))
        question_specs = self._question_specs(questions, legal_routes)
        state_digest = canonical_hash(state.to_dict())
        question_bundle_digest = canonical_hash([item.to_dict() for item in question_specs])
        request_id = f"system-one-{uuid.uuid4()}"
        request_digest = canonical_hash({
            "identity": identity.to_dict(), "sequence": sequence,
            "state_digest": state_digest,
            "question_bundle_digest": question_bundle_digest,
            "policy_hash": policy_hash,
        })
        return SystemOneFrame(
            identity=identity, request_id=request_id, sequence=sequence,
            state_digest=state_digest, question_bundle_digest=question_bundle_digest,
            request_digest=request_digest, policy_hash=policy_hash,
            backend_id=policy.backend_id, model_id=policy.model_id,
            model_revision=policy.model_revision, model_content_hash=policy.model_content_hash,
            provider_protocol_hash=policy.provider_protocol_hash,
            calibration_bundle_hash=policy.calibration_bundle_hash,
            state=state, questions=question_specs)


__all__ = ["SystemOneStateProjector"]
