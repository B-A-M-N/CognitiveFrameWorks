"""Immutable task request contract shared by host adapters and resolver."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One host-owned claim that defines when the requested task is done."""

    criterion_id: str
    claim: str
    kind: str
    required: bool = True
    verification_strength: str = "host_observation"

    def __post_init__(self) -> None:
        for name in ("criterion_id", "claim", "kind", "verification_strength"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError(f"{name} must be non-empty bounded text")

    def to_dict(self) -> dict[str, Any]:
        return {"criterion_id": self.criterion_id, "claim": self.claim,
                "kind": self.kind, "required": self.required,
                "verification_strength": self.verification_strength}


@dataclass(frozen=True)
class VerificationPolicy:
    """Task-specific limits for support work and recovery."""

    support_actions_before_goal_reanchor: int = 5
    max_recovery_epochs_without_goal_progress: int = 3
    max_same_approach_retries: int = 2

    def __post_init__(self) -> None:
        for name in ("support_actions_before_goal_reanchor",
                     "max_recovery_epochs_without_goal_progress",
                     "max_same_approach_retries"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1 or value > 100:
                raise ValueError(f"{name} must be an integer between 1 and 100")

    def to_dict(self) -> dict[str, int]:
        return {"support_actions_before_goal_reanchor": self.support_actions_before_goal_reanchor,
                "max_recovery_epochs_without_goal_progress": self.max_recovery_epochs_without_goal_progress,
                "max_same_approach_retries": self.max_same_approach_retries}


@dataclass(frozen=True)
class TaskGoalContract:
    """Immutable statement of the user outcome and the claims needed to finish."""

    objective: str
    deliverable_kind: str
    work_mode: str
    acceptance_criteria: Tuple[AcceptanceCriterion, ...] = ()
    non_goals: Tuple[str, ...] = ()
    task_complexity: str = "standard"
    verification_policy: VerificationPolicy = field(default_factory=VerificationPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.objective, str) or not self.objective.strip() or len(self.objective) > 4096:
            raise ValueError("objective must be non-empty bounded text")
        if self.work_mode not in {"delivery", "diagnose", "review", "research"}:
            raise ValueError("work_mode must be delivery, diagnose, review, or research")
        if self.deliverable_kind not in {
                "visual_ui", "code_behavior", "document", "architecture",
                "test_suite", "infrastructure", "analysis"}:
            raise ValueError("unsupported deliverable_kind")
        if self.task_complexity not in {"trivial", "standard", "complex"}:
            raise ValueError("task_complexity must be trivial, standard, or complex")
        criteria = tuple(self.acceptance_criteria)
        if any(not isinstance(item, AcceptanceCriterion) for item in criteria):
            raise ValueError("acceptance_criteria must contain AcceptanceCriterion values")
        identifiers = [item.criterion_id for item in criteria]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("acceptance criterion IDs must be unique")
        if not any(item.required for item in criteria):
            raise ValueError("task goal contract requires at least one required acceptance criterion")
        non_goals = tuple(self.non_goals)
        if any(not isinstance(item, str) or not item.strip() or len(item) > 1024
               for item in non_goals):
            raise ValueError("non_goals must contain non-empty bounded text")
        if not isinstance(self.verification_policy, VerificationPolicy):
            if isinstance(self.verification_policy, Mapping):
                object.__setattr__(self, "verification_policy",
                                   VerificationPolicy(**dict(self.verification_policy)))
            else:
                raise ValueError("verification_policy must be a VerificationPolicy")
        object.__setattr__(self, "acceptance_criteria", criteria)
        object.__setattr__(self, "non_goals", non_goals)

    def to_dict(self) -> dict[str, Any]:
        return {"objective": self.objective,
                "deliverable_kind": self.deliverable_kind,
                "work_mode": self.work_mode,
                "acceptance_criteria": [item.to_dict() for item in self.acceptance_criteria],
                "non_goals": list(self.non_goals),
                "task_complexity": self.task_complexity,
                "verification_policy": self.verification_policy.to_dict()}

    @property
    def contract_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskRequest:
    task_id: str
    # Trusted host identity.  These fields are deliberately part of the
    # request rather than model-controlled observation context: learned
    # routing must never cross application or producer boundaries by accident.
    application_id: str
    subject_ref: Optional[str] = None
    phase: Optional[str] = None
    shape: str = "implement"
    domain_tags: Tuple[str, ...] = ()
    operation: Optional[str] = None
    trigger: Optional[str] = None
    model: Optional[str] = None
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    model_revision: Optional[str] = None
    model_capability_hash: Optional[str] = None
    harness: Optional[str] = None
    harness_version: Optional[str] = None
    toolset: Optional[str] = None
    observation_context: Mapping[str, Any] = field(default_factory=dict)
    application_version: Optional[str] = None
    application_instance_id: Optional[str] = None
    namespace_id: str = "default"
    agent_id: str = "agent"
    agent_instance_id: Optional[str] = None
    attempt_id: str = "attempt-1"
    interaction_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    delegator_agent_id: Optional[str] = None
    delegate_agent_id: Optional[str] = None
    delegation_id: Optional[str] = None
    role: Optional[str] = None
    parent_decision_ref: Optional[str] = None
    shared_evidence_refs: Tuple[str, ...] = ()
    delegation_topology: Optional[str] = None
    premise_digest: Optional[str] = None
    failure_attribution: str = "none"
    adopted_profile_hashes: Tuple[str, ...] = ()
    shared_workflow_membership: Optional[Mapping[str, Any]] = None
    policy_revocation_revision: int = 0
    revocation_mode: str = "deferred"
    # Host-predeclared adaptive experiment.  This is never populated from
    # model output; it is part of the trusted task-start request.
    routing_experiment_plan: Mapping[str, Any] = field(default_factory=dict)
    # Host-owned action extensions are composed and hashed before resolution.
    # The model-facing request cannot mutate this mapping after construction.
    action_registry: Mapping[str, Any] = field(default_factory=dict, repr=False)
    goal_contract: Optional[TaskGoalContract] = None

    def __post_init__(self) -> None:
        for name in ("task_id", "application_id", "namespace_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{name} must be a non-empty bounded identifier")
        if self.model is not None and self.model_id is not None and self.model != self.model_id:
            raise ValueError("model and model_id must agree when both are provided")
        canonical_model = self.model_id or self.model
        object.__setattr__(self, "model_id", canonical_model)
        object.__setattr__(self, "model", canonical_model)
        object.__setattr__(self, "application_instance_id",
                           self.application_instance_id or self.application_id)
        if not self.agent_id or len(self.agent_id) > 256:
            raise ValueError("agent_id must be a non-empty bounded identifier")
        object.__setattr__(self, "agent_instance_id",
                           self.agent_instance_id or self.agent_id)
        if not self.attempt_id or len(self.attempt_id) > 256:
            raise ValueError("attempt_id must be a non-empty bounded identifier")
        if self.failure_attribution not in {"none", "inherited", "independent", "external", "pending"}:
            raise ValueError("failure_attribution has an unsupported value")
        if any(not isinstance(item, str) or not item or len(item) > 256 for item in self.shared_evidence_refs):
            raise ValueError("shared_evidence_refs must contain bounded references")
        for name in ("parent_decision_ref", "delegation_topology", "premise_digest"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value or len(value) > 256):
                raise ValueError(f"{name} must be a bounded reference")
        object.__setattr__(self, "shared_evidence_refs", tuple(self.shared_evidence_refs))
        object.__setattr__(self, "adopted_profile_hashes", tuple(self.adopted_profile_hashes))
        if self.shared_workflow_membership is not None:
            membership = dict(self.shared_workflow_membership)
            required = {"namespace_id", "application_id", "application_instance_id",
                        "workflow_id", "statework_id", "subject_ref"}
            if not required.issubset(membership) or any(
                    not isinstance(membership.get(name), str) or not membership.get(name)
                    for name in required):
                raise ValueError("shared workflow membership has an invalid identity contract")
            revision = membership.get("membership_revision", membership.get("authorization_revision"))
            if int(revision or 0) < 1:
                raise ValueError("shared workflow membership requires a positive membership revision")
            object.__setattr__(self, "shared_workflow_membership", MappingProxyType(membership))
        if self.policy_revocation_revision < 0:
            raise ValueError("policy_revocation_revision must be non-negative")
        if self.revocation_mode not in {"deferred", "immediate"}:
            raise ValueError("revocation_mode must be deferred or immediate")
        object.__setattr__(self, "domain_tags", tuple(self.domain_tags))
        object.__setattr__(self, "observation_context",
                           MappingProxyType(dict(self.observation_context)))
        object.__setattr__(self, "action_registry",
                           MappingProxyType(dict(self.action_registry)))
        object.__setattr__(self, "routing_experiment_plan",
                           MappingProxyType(dict(self.routing_experiment_plan)))
        contract = self.goal_contract
        if contract is None and isinstance(self.observation_context.get("goal_contract"), Mapping):
            contract = self.observation_context["goal_contract"]
        if contract is not None and not isinstance(contract, TaskGoalContract):
            if not isinstance(contract, Mapping):
                raise ValueError("goal_contract must be a TaskGoalContract or mapping")
            values = dict(contract)
            values["acceptance_criteria"] = tuple(
                item if isinstance(item, AcceptanceCriterion) else AcceptanceCriterion(**dict(item))
                for item in values.get("acceptance_criteria", ()))
            values["verification_policy"] = VerificationPolicy(**dict(values.get("verification_policy") or {}))
            contract = TaskGoalContract(**values)
        object.__setattr__(self, "goal_contract", contract)
