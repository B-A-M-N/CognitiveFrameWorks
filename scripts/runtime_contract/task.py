"""Immutable task request contract shared by host adapters and resolver."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple


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
                        "workflow_id", "statework_id", "subject_ref", "authorization_revision"}
            if set(membership) != required or any(
                    not isinstance(membership.get(name), str) or not membership.get(name)
                    for name in required - {"authorization_revision"}):
                raise ValueError("shared workflow membership has an invalid identity contract")
            if int(membership.get("authorization_revision") or 0) < 1:
                raise ValueError("shared workflow membership requires a positive authorization revision")
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
