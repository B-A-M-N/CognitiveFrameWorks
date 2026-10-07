"""Host ingress and authority ports for a CFW runtime session."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol

from runtime_contract.actions import ActionObjectiveBinding, AgentActionRequest
from runtime_contract.convergence import AttemptDescriptor
from runtime_contract.evidence import TerminationPermit
from runtime_contract.statework_provider import DEFAULT_STATEWORK_ROOT, statework_provider
from runtime_session.lifecycle import (
    ActionPreparation,
)
from runtime_session.standalone_route_policy import (
    ChildPolicyEpoch, RouteReplanRecommendation,
)

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

    def bind_action_objective(self, action: Mapping[str, Any] | AgentActionRequest, *,
                              purpose_class: str, criterion_ids: Iterable[str] = (),
                              cause_id: str = "primary-objective",
                              artifact_revision: Optional[int] = None
                              ) -> ActionObjectiveBinding:
        """Bind one exact action to host-accepted task criteria."""
        if purpose_class == "REQUIRED_VERIFICATION" and artifact_revision is None:
            artifact_revision = self._session.behavior.task_evidence.artifact_revision
        return self._session._bind_action_objective(
            self._capability, action, purpose_class=purpose_class,
            criterion_ids=criterion_ids, cause_id=cause_id,
            artifact_revision=artifact_revision)

    def prepare_action(self, action: Mapping[str, Any] | AgentActionRequest, *,
                       objective_binding: Optional[ActionObjectiveBinding] = None
                       ) -> ActionPreparation:
        """Prepare an action with an optional binding issued by this host session."""
        return self._session._prepare_action(
            action, capability=self._capability,
            objective_binding=objective_binding)

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
                           actual_result: Optional[Mapping[str, Any]] = None,
                           covered_boundary_ids: Iterable[str] = ()) -> str:
        return self._session._record_tool_result(
            self._capability, tool_type, result_class, subject_ref,
            invocation_id=invocation_id, result_digest=result_digest,
            action_request_id=action_request_id,
            actual_result=actual_result,
            covered_boundary_ids=covered_boundary_ids)

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

    def record_criterion_verified(self, *, criterion_id: str,
                                  evidence_ref: str) -> None:
        self._session._record_criterion_verified(
            self._capability, criterion_id=criterion_id,
            evidence_ref=evidence_ref)

    def rank_legal_routes(
            self, legal_children: Mapping[str, Any], *,
            state_revision: int, target_workflow_id: str,
            route_preference: Optional[str] = None,
            preference_source: str = "metacognition") -> Optional[RouteReplanRecommendation]:
        return self._session._rank_legal_routes(
            self._capability, legal_children=legal_children,
            state_revision=state_revision, target_workflow_id=target_workflow_id,
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
                                *, claim_digest: Optional[str] = None,
                                covered_boundary_ids: Iterable[str] = ()) -> str:
        return self._session._record_validator_result(
            self._capability, validator_id, invocation_id, result, subject_ref,
            claim_digest=claim_digest,
            covered_boundary_ids=covered_boundary_ids)

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
                                   subject_ref: Optional[str] = None, *,
                                   covered_boundary_ids: Iterable[str] = ()) -> str:
        return self._session._record_repository_evidence(
            self._capability, packet, validation, subject_ref=subject_ref,
            covered_boundary_ids=covered_boundary_ids)

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
class StateAuthority(Protocol):
    """Engine-neutral host interface for authoritative workflow state."""
    def inspect(self, *, statework_id: str, subject_ref: str, contract_hash: str) -> Dict[str, Any]:
        raise NotImplementedError
    def commit(self, *, statework_id: str, subject_ref: str, contract_hash: str,
               expected_revision: int, output_state: str, evidence_refs: list[str], trigger: str,
               evidence_records: Optional[list[Mapping[str, Any]]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def workflow_contract_identity(self, statework_id: str,
                                   contract_hash: str) -> Dict[str, Any]:
        raise NotImplementedError

    def workflow_authority_identity(self, *, statework_id: str,
                                    subject_ref: str, contract_hash: str) -> Any:
        """Return CSW's immutable identity for one bound workflow scope."""
        raise NotImplementedError

    def inspect_workflow(self, *, statework_id: str, subject_ref: str,
                         contract_hash: str) -> Any:
        raise NotImplementedError

    def resolve_legacy_step(self, *, statework_id: str, subject_ref: str,
                            contract_hash: str, to_state: str,
                            trigger: Optional[str]) -> str:
        raise NotImplementedError

    def apply_workflow_step(self, *, statework_id: str, subject_ref: str,
                            contract_hash: str, step_id: str,
                            expected_revision: int,
                            evidence_records: Optional[list[Mapping[str, Any]]] = None) -> Any:
        raise NotImplementedError
def create_state_authority(root: Optional[Path] = None, **options: Any) -> StateAuthority:
    """Create the optional provider's adapter for CFW's neutral state port."""
    provider = statework_provider(
        Path(root or DEFAULT_STATEWORK_ROOT).resolve())
    factory = getattr(provider, "create_state_authority", None)
    if not callable(factory):
        raise RuntimeError(
            "configured StateWork provider does not expose a StateAuthority adapter")
    return factory(**options)
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
        if grant.get("member_agent_id") != session.identity_authority.identity.agent_instance_id:
            return False
        if operation not in set(grant.get("allowed_operations") or ()):
            return False
        try:
            expiry = datetime.fromisoformat(str(grant.get("expires_at")).replace("Z", "+00:00"))
        except ValueError:
            return False
        return expiry > datetime.now(timezone.utc)
