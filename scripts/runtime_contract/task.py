"""Immutable task request contract shared by host adapters and resolver."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class TaskRequest:
    task_id: str
    subject_ref: Optional[str] = None
    phase: Optional[str] = None
    shape: str = "implement"
    domain_tags: Tuple[str, ...] = ()
    operation: Optional[str] = None
    trigger: Optional[str] = None
    model: Optional[str] = None
    harness: Optional[str] = None
    toolset: Optional[str] = None
    observation_context: Mapping[str, Any] = field(default_factory=dict)
    agent_id: str = "agent"
    agent_instance_id: Optional[str] = None
    attempt_id: str = "attempt-1"
    interaction_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    delegator_agent_id: Optional[str] = None
    delegate_agent_id: Optional[str] = None
    delegation_id: Optional[str] = None
    role: Optional[str] = None
    # Host-owned action extensions are composed and hashed before resolution.
    # The model-facing request cannot mutate this mapping after construction.
    action_registry: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.agent_id or len(self.agent_id) > 256:
            raise ValueError("agent_id must be a non-empty bounded identifier")
        object.__setattr__(self, "agent_instance_id",
                           self.agent_instance_id or self.agent_id)
        if not self.attempt_id or len(self.attempt_id) > 256:
            raise ValueError("attempt_id must be a non-empty bounded identifier")
        object.__setattr__(self, "domain_tags", tuple(self.domain_tags))
        object.__setattr__(self, "observation_context",
                           MappingProxyType(dict(self.observation_context)))
        object.__setattr__(self, "action_registry",
                           MappingProxyType(dict(self.action_registry)))
