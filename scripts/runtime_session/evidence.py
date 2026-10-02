"""Evidence, workflow event, and session journal methods for CFW."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from runtime_contract.actions import ActionDescriptor, ActionObjectiveBinding, AgentActionRequest
from runtime_contract.behavioral_fusion import fuse_progress_relation
from runtime_contract.evidence import EvidenceAttestation, EvidenceRecord, StateSegment
from runtime_contract.handlers import handler_for_enforcement
from runtime_contract.stateworks import SegmentStatus
from runtime_contract.system_one import ActivityFact, SystemOneJudgment
from runtime_contract.validators import evaluate_validator, validator_registry_hash
from runtime_session.host_authority import _HostCapability
from runtime_session.lifecycle import GateDecision
from runtime_session.telemetry import HostTelemetry

from runtime_session.resolver_bridge import resolve_runtime

AUTHORITATIVE_EVIDENCE_KINDS = {
    "repository_truth", "authority", "repository_evidence", "rollback", "rollback_plan",
}


class EvidenceMethods:
    """Evidence and workflow methods over RuntimeSession's shared state."""
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
        provider = resolve_runtime.statework_provider(
            Path(self.bundle.statework_root or resolve_runtime.DEFAULT_STATEWORK_ROOT))
        if packet.get("packet_type") != "repository_truth_packet":
            raise ValueError("repository truth adapter accepts only repository truth packets")
        if not provider.validate_repository_truth(dict(packet), observation_context):
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
    def _bind_action_objective(
            self, capability: _HostCapability,
            action: Mapping[str, Any] | AgentActionRequest, *,
            purpose_class: str, criterion_ids: Iterable[str], cause_id: str,
            artifact_revision: Optional[int]) -> ActionObjectiveBinding:
        self._require_host(capability)
        request = self._request(action)
        criteria = tuple(criterion_ids)
        known = {item.criterion_id for item in self.metacognitive_state.criteria}
        if not set(criteria).issubset(known):
            raise ValueError("objective binding references an unknown task criterion")
        if artifact_revision is not None and artifact_revision != self.metacognitive_state.artifact_revision:
            raise ValueError("objective binding references a stale artifact revision")
        binding = ActionObjectiveBinding(
            action_digest=request.digest, purpose_class=purpose_class,
            criterion_ids=criteria, cause_id=cause_id,
            artifact_revision=artifact_revision)
        self._action_objective_bindings[binding.binding_digest] = binding
        while len(self._action_objective_bindings) > 512:
            self._action_objective_bindings.pop(next(iter(self._action_objective_bindings)))
        self._journal("action_objective_binding_issued",
                      binding=binding.to_dict(), subject_ref=request.subject_ref)
        return binding
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
        provider = resolve_runtime.statework_provider(
            Path(self.bundle.statework_root or resolve_runtime.DEFAULT_STATEWORK_ROOT))
        evidence_registry = provider.evidence_kind_registry()
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
        root = Path(self.bundle.statework_root or resolve_runtime.DEFAULT_STATEWORK_ROOT)
        provider = resolve_runtime.statework_provider(root)
        registry = provider.evidence_kind_registry()
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
            "accounting_lane": str((pending or {}).get("accounting_lane") or "UNCLASSIFIED"),
            "objective_binding": dict((pending or {}).get("objective_binding") or {}),
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
            lane = str((pending or {}).get("accounting_lane") or "UNCLASSIFIED")
            state = self.convergence.record_committed_action(
                cause_id="primary-objective", lane=lane)
            self._record_convergence("committed_action")
            self._emit_runtime({
                "category": "tool_result", "event_type": "action_accounting_recorded",
                "subject": subject_ref, "evidence_refs": [evidence_id],
                "payload": {
                    "summary": "host-correlated committed action entered task convergence ledger",
                    "result_class": lane.lower(),
                    "accounting_lane": lane,
                    "total_actions_since_goal_progress": (
                        state.total_actions_since_goal_progress),
                    "support_actions_since_goal_progress": (
                        state.support_actions_since_goal_progress),
                }})
            if lane == "SUPPORT":
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
                               "result_class": evidence_type,
                               "progress_class": "VALIDATION_EVIDENCE"}},
            invocation_id=invocation_id,
            content={"validator_id": validator_id, "invocation_id": invocation_id,
                     "tool_evidence_id": invocation["evidence_id"],
                     "result": typed_result},
            claim_digest=claim_digest, schema_id=f"validator-result:{evidence_type}",
            validator_id=validator_id)
        self.metacognitive_state.record_activity(
            "validation", source="validator", result_class=evidence_type,
            progress_class="VALIDATION_EVIDENCE", evidence_kinds=(evidence_type,))
        if self.convergence is not None:
            self.convergence.record_progress(
                cause_id="primary-objective",
                progress_digest=hashlib.sha256(json.dumps({
                    "validator_id": validator_id, "evidence_id": evidence_id,
                    "result_class": evidence_type,
                }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
                progress_class="VALIDATION_EVIDENCE", evidence_ref=evidence_id)
            self._record_convergence("validation_evidence")
        return evidence_id
    def record_state_transition(self, statework_id: str, subject_ref: str,
                                output_state: str, *,
                                trigger: Optional[str] = None,
                                evidence_refs: Optional[List[str]] = None,
                                input_state: Optional[str] = None) -> GateDecision:
        """Compatibility adapter for callers that still speak in states.

        Workflow semantics belong to CSW. This adapter translates a legacy
        destination-state request into an opaque step ID, then delegates the
        operation to the engine-neutral workflow API.
        """
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
        if statework is None or statework_id not in self.active_statework_ids:
            self._emit_runtime({"category": "blocker", "event_type": "inactive_statework_transition",
                       "subject": subject_ref,
                       "payload": {"summary": str(statework_id)[:128],
                                   "result_class": "blocked"}})
            return GateDecision.BLOCK
        if self.state_authority is None or not all(callable(getattr(self.state_authority, name, None))
                for name in ("inspect_workflow", "resolve_legacy_step")):
            self._emit_runtime({"category": "blocker", "event_type": "workflow_authority_unavailable",
                       "subject": subject_ref,
                       "payload": {"summary": "generic WorkflowAuthority is unavailable",
                                   "result_class": "blocked"}})
            return GateDecision.BLOCK
        contract_hash = str(statework.get("workflow_contract_hash") or "")
        if not contract_hash:
            raise ValueError(f"StateWork {statework_id!r} has no pinned workflow contract")
        try:
            snapshot = self.state_authority.inspect_workflow(
                statework_id=statework_id, subject_ref=subject_ref,
                contract_hash=contract_hash)
        except Exception as exc:
            self._emit_runtime({"category": "blocker", "event_type": "workflow_authority_unavailable",
                       "subject": subject_ref,
                       "payload": {"summary": str(exc)[:512], "result_class": "blocked"}})
            return GateDecision.BLOCK
        if input_state is not None and input_state != snapshot.current_state:
            self._emit_runtime({"category": "blocker", "event_type": "state_conflict",
                       "subject": subject_ref, "input_state": input_state,
                       "output_state": output_state,
                       "payload": {"summary": "tracked state differs from asserted origin",
                                   "result_class": "re-observation required"}})
            return GateDecision.REQUIRE_REINSPECTION
        try:
            step_id = self.state_authority.resolve_legacy_step(
                statework_id=statework_id, subject_ref=subject_ref,
                contract_hash=contract_hash, to_state=output_state,
                trigger=trigger)
        except ValueError as exc:
            self._emit_runtime({"category": "blocker", "event_type": "legacy_workflow_step_unavailable",
                       "subject": subject_ref,
                       "payload": {"summary": str(exc)[:512], "result_class": "blocked"}})
            return GateDecision.BLOCK
        return self.record_workflow_step(
            statework_id, subject_ref, step_id,
            evidence_refs=evidence_refs,
            expected_revision=int(snapshot.revision))
    def record_workflow_step(self, statework_id: str, subject_ref: str,
                             step_id: str, *,
                             evidence_refs: Optional[List[str]] = None,
                             expected_revision: Optional[int] = None) -> GateDecision:
        """Apply one engine-neutral workflow step through CSW authority."""
        statework = next((item for item in self.bundle.stateworks
                          if item.get("id") == statework_id), None)
        if statework is None or statework_id not in self.active_statework_ids:
            self._emit_runtime({"category": "blocker", "event_type": "inactive_statework_transition",
                       "subject": subject_ref,
                       "payload": {"summary": str(statework_id)[:128],
                                   "result_class": "blocked"}})
            return GateDecision.BLOCK
        bound_subject = statework.get("subject_ref") or self.bundle.policy.subject_ref
        if bound_subject and str(bound_subject) != subject_ref:
            return GateDecision.BLOCK
        if not self._membership_current(statework_id, subject_ref):
            return GateDecision.BLOCK
        authority = self.state_authority
        if authority is None or not callable(getattr(authority, "inspect_workflow", None)):
            self._emit_runtime({"category": "blocker", "event_type": "workflow_authority_unavailable",
                       "subject": subject_ref,
                       "payload": {"summary": "generic WorkflowAuthority is unavailable",
                                   "result_class": "blocked"}})
            return GateDecision.BLOCK
        contract_hash = str(statework.get("workflow_contract_hash") or "")
        if not contract_hash:
            raise ValueError(f"StateWork {statework_id!r} has no pinned workflow contract")
        try:
            snapshot = authority.inspect_workflow(
                statework_id=statework_id, subject_ref=subject_ref,
                contract_hash=contract_hash)
            revision = int(snapshot.revision)
            if expected_revision is not None and revision != int(expected_revision):
                raise ValueError("workflow revision changed; re-inspection required")
            verified_evidence = self.ledger.resolve(
                list(evidence_refs or ()), subject_ref=subject_ref)
            decision = authority.apply_workflow_step(
                statework_id=statework_id, subject_ref=subject_ref,
                contract_hash=contract_hash, step_id=step_id,
                expected_revision=revision, evidence_records=verified_evidence)
        except Exception as exc:
            self._emit_runtime({"category": "blocker", "event_type": "workflow_authority_conflict",
                       "subject": subject_ref,
                       "payload": {"summary": str(exc)[:512],
                                   "result_class": "re-inspection required"}})
            return GateDecision.REQUIRE_REINSPECTION
        if not decision.allowed:
            self._emit_runtime({"category": "state_transition", "event_type": "workflow_step_blocked",
                       "subject": subject_ref,
                       "payload": {"summary": decision.reason[:512],
                                   "workflow_engine": decision.engine_kind,
                                   "step_id": decision.step_id,
                                   "result_class": "blocked"}})
            return GateDecision.BLOCK

        evidence_ids = [str(item["ref"]) for item in verified_evidence]
        metadata = decision.to_dict()
        metadata.update({"edge_id": decision.step_id,
                         "work_class": decision.work_class,
                         "domain_effect": decision.domain_effect,
                         "engine_kind": decision.engine_kind})
        prior_state, next_state = decision.previous_state, decision.current_state
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if prior_state is not None and next_state is not None:
            state_key = (statework_id, subject_ref)
            self.statework_states[state_key] = str(next_state)
            self.statework_entry_times[state_key] = timestamp
            self.metacognitive_state.record_workflow_progress(
                statework_id=statework_id, subject_ref=subject_ref,
                engine_kind=decision.engine_kind,
                workflow_revision=decision.revision, step_id=decision.step_id,
                progress_class=decision.progress_class, completed=decision.completed,
                work_class=decision.work_class, domain_effect=decision.domain_effect,
                current_position=str(next_state), previous_position=str(prior_state),
                evidence_refs=evidence_ids)
            prior_revision = sum(
                1 for item in self.state_segments
                if item.statework_id == statework_id and item.subject_ref == subject_ref)
            segment = StateSegment(
                segment_id=f"segment-{uuid.uuid4()}", task_id=self.bundle.task_id,
                subject_ref=subject_ref, input_state=str(prior_state),
                output_state=str(next_state), evidence_refs=tuple(evidence_ids),
                recorded_at=timestamp, statework_id=statework_id)
            self.state_segments.append(segment)
            self._journal("state_transition_allowed", statework_id=statework_id,
                          subject_ref=subject_ref, input_state=str(prior_state),
                          output_state=str(next_state), evidence_refs=evidence_ids,
                          recorded_at=timestamp, segment_id=segment.segment_id,
                          transition=metadata, progress_class="WORKFLOW_PROGRESS",
                          csw_progress_class=decision.progress_class)
        else:
            self.metacognitive_state.record_workflow_progress(
                statework_id=statework_id, subject_ref=subject_ref,
                engine_kind=decision.engine_kind, step_id=decision.step_id,
                workflow_revision=decision.revision, progress_class=decision.progress_class,
                completed=decision.completed, work_class=decision.work_class,
                domain_effect=decision.domain_effect, evidence_refs=evidence_ids)
            self._journal("workflow_step_allowed", statework_id=statework_id,
                          subject_ref=subject_ref, engine_kind=decision.engine_kind,
                          step_id=decision.step_id, revision=decision.revision,
                          progress_class=decision.progress_class,
                          completed=decision.completed, evidence_refs=evidence_ids,
                          work_class=decision.work_class,
                          domain_effect=decision.domain_effect, recorded_at=timestamp)
        if self.convergence is not None:
            progress_digest = hashlib.sha256(json.dumps({
                "statework_id": statework_id, "subject_ref": subject_ref,
                "engine_kind": decision.engine_kind, "step_id": decision.step_id,
                "revision": decision.revision,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            self.convergence.record_progress(
                cause_id="primary-objective", progress_digest=progress_digest,
                progress_class="WORKFLOW_PROGRESS",
                evidence_ref=evidence_ids[-1] if evidence_ids else None)
        self._emit_runtime({"category": "state_transition",
                    "event_type": "workflow_step_progress",
                    "subject": subject_ref,
                    "payload": {"summary": "CSW workflow step was applied",
                                "csw_progress_class": decision.progress_class,
                                "progress_class": "WORKFLOW_PROGRESS"}})
        self._record_convergence("workflow_progress")
        return GateDecision.ALLOW
    def _publish_handoff(self, capability: _HostCapability, packet: Dict[str, Any], *,
                         observation_context: Optional[Mapping[str, Any]] = None) -> str:
        self._require_host(capability)
        if packet.get("task_id") != self.bundle.task_id:
            raise ValueError("handoff packet belongs to another task")
        expected_subject = (self.current_segment.subject_ref
                            or self.bundle.policy.subject_ref)
        if not expected_subject or packet.get("subject_ref") != expected_subject:
            raise ValueError("handoff packet subject is not the active StateWork subject")
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
        producer_workflow = next(
            (item for item in self.bundle.stateworks
             if item.get("id") == active_producer), None)
        if producer_workflow is None:
            raise PermissionError("handoff producer has no active workflow contract")
        contract_hash = str(producer_workflow.get("workflow_contract_hash") or "")
        workflow_complete = False
        if self.state_authority is not None and contract_hash:
            snapshot = self.state_authority.inspect_workflow(
                statework_id=str(active_producer), subject_ref=expected_subject,
                contract_hash=contract_hash)
            workflow_complete = bool(snapshot.completed)
        else:
            workflow_fact = next((item for item in self.metacognitive_state.workflows
                                  if item.statework_id == active_producer
                                  and item.subject_ref == expected_subject), None)
            workflow_complete = bool(workflow_fact and workflow_fact.completed)
        if not workflow_complete:
            raise PermissionError("handoff requires the producer workflow to be complete")
        if self._packet_store is None:
            provider = resolve_runtime.statework_provider(
                Path(self.bundle.statework_root or resolve_runtime.DEFAULT_STATEWORK_ROOT))
            packet_root = resolve_runtime.runtime_state_dir() / "packets"
            safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.bundle.task_id)[:128] or "task"
            self._packet_store = provider.packet_store(
                None, storage_path=packet_root / f"{safe_task}.json")
        provider = resolve_runtime.statework_provider(
            Path(self.bundle.statework_root or resolve_runtime.DEFAULT_STATEWORK_ROOT))
        packet_meta = provider.packet_metadata(str(packet.get("packet_type") or ""))
        freshness_contract = packet_meta.get("freshness")
        if freshness_contract is None and packet_meta.get("freshness_validator"):
            freshness_contract = {"strategy": "trusted_validator",
                                 "validator_id": packet_meta.get("freshness_validator")}
        if freshness_contract not in (None, "immutable") and observation_context is None:
            raise ValueError("freshness validation context is mandatory for this handoff packet")
        if freshness_contract is None:
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
            provider = resolve_runtime.statework_provider(
                Path(self.bundle.statework_root or resolve_runtime.DEFAULT_STATEWORK_ROOT))
            packet_root = resolve_runtime.runtime_state_dir() / "packets"
            safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.bundle.task_id)[:128] or "task"
            self._packet_store = provider.packet_store(
                None, storage_path=packet_root / f"{safe_task}.json")
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
