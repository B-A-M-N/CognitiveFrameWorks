"""Construct cohesive CFW session state from validated runtime inputs."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from runtime_contract.actions import ActionDescriptor
from runtime_contract.convergence import ConvergenceController
from runtime_contract.evidence import EvidenceLedger
from runtime_contract.stateworks import ExecutionSegment
from runtime_contract.system_one import BehavioralJudge
from runtime_session.behavior_authority import StandaloneBehaviorAuthority
from runtime_session.lifecycle import ExecutionIdentity
from runtime_session.session import RuntimeSession
from runtime_session.standalone_route_policy import (
    PolicyEpochRecord, StandaloneRoutePolicy,
)
from runtime_session.state import (
    ActionState, AttemptConvergenceState, AuthorityBindings, BehaviorState,
    EvidenceState, IdentityAuthorityState, LifecycleWorkflowState,
    SessionIdentity, WorkflowExecutionPlan,
)
from runtime_start.identity import StartIdentity


def build_runtime_session(
        bundle: Any, *, identity: StartIdentity,
        action_registry: Mapping[str, ActionDescriptor],
        behavioral_judge: BehavioralJudge | None,
        policy_epoch_record: PolicyEpochRecord | None,
        ) -> RuntimeSession:
    """Create each subsystem aggregate before binding external authorities."""
    execution_segments = tuple(
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
    ) or (ExecutionSegment("segment-0", 0, None, bundle.policy.subject_ref),)

    pinned = bundle.pinned
    session_id = identity.session_id
    agent_instance_id = pinned.get("agent_instance_id", pinned.get("agent_id", "agent"))
    attempt_id = pinned.get("attempt_id", "attempt-1")
    interaction_id = pinned.get("interaction_id")
    execution_identity = ExecutionIdentity(
        namespace_id=pinned["namespace_id"],
        application_instance_id=pinned["application_instance_id"],
        agent_instance_id=agent_instance_id,
        session_id=session_id,
        task_id=bundle.task_id,
        attempt_id=attempt_id,
        interaction_id=interaction_id,
    )
    memberships = tuple(MappingProxyType(dict(item)) for item in
                        pinned.get("shared_workflow_memberships", ()))
    identity_state = SessionIdentity(
        session_nonce=identity.session_nonce,
        session_id=session_id,
        agent_instance_id=agent_instance_id,
        attempt_id=attempt_id,
        interaction_id=interaction_id,
        parent_session_id=pinned.get("parent_session_id"),
        delegator_agent_id=pinned.get("delegator_agent_id"),
        delegate_agent_id=pinned.get("delegate_agent_id"),
        delegation_id=pinned.get("delegation_id"),
        role=pinned.get("role"),
        parent_decision_ref=pinned.get("parent_decision_ref"),
        shared_evidence_refs=tuple(pinned.get("shared_evidence_refs") or ()),
        delegation_topology=pinned.get("delegation_topology"),
        premise_digest=pinned.get("premise_digest"),
        failure_attribution=pinned.get("failure_attribution") or "none",
        adopted_profile_hashes=tuple(pinned.get("adopted_profile_hashes") or ()),
        execution_identity=execution_identity,
        host_context=identity.host_context,
    )
    authority_bindings = AuthorityBindings(
        shared_workflow_membership=pinned.get("shared_workflow_membership"),
        shared_workflow_memberships=memberships,
    )
    evidence_writer_token = object()
    evidence_state = EvidenceState(
        ledger=EvidenceLedger(bundle.task_id, writer_token=evidence_writer_token),
        _writer_token=evidence_writer_token)
    behavior_state = BehaviorState(
        _authority=(StandaloneBehaviorAuthority(
            system_one_judge=behavioral_judge,
            route_policy=StandaloneRoutePolicy(policy_epoch_record))
                    if behavioral_judge is not None or policy_epoch_record is not None
                    else None))
    behavior_state.metacognitive.ensure_criteria(pinned.get("goal_contract") or {})
    verification_policy = ((pinned.get("goal_contract") or {}).get(
        "verification_policy") or {})
    convergence = ConvergenceController(
        identity={
            "namespace_id": execution_identity.namespace_id,
            "application_instance_id": execution_identity.application_instance_id,
            "agent_instance_id": execution_identity.agent_instance_id,
            "session_id": execution_identity.session_id,
            "task_id": execution_identity.task_id,
            "attempt_id": execution_identity.attempt_id,
            "interaction_id": execution_identity.interaction_id,
        },
        max_recovery_epochs_without_progress=int(
            verification_policy.get("max_recovery_epochs_without_goal_progress", 3) or 3),
        max_same_approach_retries=int(
            verification_policy.get("max_same_approach_retries", 2) or 2),
    )
    return RuntimeSession(
        bundle=bundle,
        identity_authority=IdentityAuthorityState(
            identity=identity_state, authorities=authority_bindings),
        injected={key: value[0] if isinstance(value, tuple) else value
                  for key, value in bundle.injected_capsules},
        lifecycle_workflow=LifecycleWorkflowState(
            _plan=WorkflowExecutionPlan(execution_segments)),
        actions=ActionState(_registry=dict(action_registry)),
        evidence=evidence_state,
        attempts=AttemptConvergenceState(_controller=convergence),
        behavior=behavior_state,
    )
