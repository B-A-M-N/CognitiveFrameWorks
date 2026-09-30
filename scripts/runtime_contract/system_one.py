"""CFW-owned boundary for optional semantic judgments.

The host builds frames and decides whether a judgment may affect an advisory
classification. System-1 has no permission, completion, retry-budget, or state
transition authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Tuple

from .task import SystemOnePolicy

PROTOCOL_VERSION = "1.0.0"
PROTOCOL_HASH = "abba6a690fab64dae9a7595d71209106fb14f285e7f490c2194476be19f3e048"
ACTION_RELATIONS = frozenset({
    "advances_primary_goal", "removes_concrete_blocker", "required_verification",
    "diagnostic_support", "unrelated",
})
PROGRESS_RELATIONS = frozenset({
    "goal_progress", "blocker_removed", "diagnostic_progress", "verification_progress",
    "recovery_progress", "no_progress",
})
RETRY_RELATIONS = frozenset({
    "materially_new", "same_premise_new_action", "equivalent_retry", "unrelated",
})
QUESTION_FIELDS = frozenset({"action_relation", "progress_relation", "retry_relation", "goal_drift"})


@dataclass(frozen=True)
class SystemOneIdentity:
    namespace_id: str
    application_id: str
    application_instance_id: str
    agent_id: str
    agent_instance_id: str
    session_id: str
    task_id: str
    attempt_id: str
    interaction_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"namespace_id": self.namespace_id, "application_id": self.application_id,
                "application_instance_id": self.application_instance_id,
                "agent_id": self.agent_id, "agent_instance_id": self.agent_instance_id,
                "session_id": self.session_id,
                "task_id": self.task_id, "attempt_id": self.attempt_id,
                "interaction_id": self.interaction_id}


@dataclass(frozen=True)
class SystemOneFrame:
    identity: SystemOneIdentity
    request_id: str
    sequence: int
    state_digest: str
    policy_hash: str
    backend_id: str
    model_id: str
    calibration_hash: str
    questions: Tuple[str, ...]
    objective: str
    criteria: Tuple[Tuple[str, str], ...]
    recent_activity: Tuple[str, ...]
    convergence: Tuple[Tuple[str, int], ...]
    csw_facts: Tuple[str, ...]
    candidate: Tuple[Tuple[str, str], ...]
    route_candidates: Tuple[str, ...] = ()
    protocol_version: str = PROTOCOL_VERSION
    protocol_hash: str = PROTOCOL_HASH

    def canonical_payload(self) -> dict[str, Any]:
        return {"protocol_version": self.protocol_version,
                "identity": self.identity.to_dict(), "request_id": self.request_id,
                "sequence": self.sequence, "state_digest": self.state_digest,
                "policy_hash": self.policy_hash, "backend_id": self.backend_id,
                "model_id": self.model_id, "calibration_hash": self.calibration_hash,
                "protocol_hash": self.protocol_hash,
                "questions": list(self.questions), "objective": self.objective,
                "criteria": [{"criterion_id": key, "claim": value}
                             for key, value in self.criteria],
                "recent_activity": list(self.recent_activity),
                "convergence": dict(self.convergence), "csw_facts": list(self.csw_facts),
                "candidate": dict(self.candidate),
                "route_candidates": list(self.route_candidates)}


@dataclass(frozen=True)
class SystemOneJudgment:
    identity: SystemOneIdentity
    request_id: str
    sequence: int
    state_digest: str
    policy_hash: str
    backend_id: str
    model_id: str
    calibration_hash: str
    confidence: float
    action_relation: Optional[str] = None
    progress_relation: Optional[str] = None
    retry_relation: Optional[str] = None
    goal_drift: Optional[str] = None
    route_preference: Optional[str] = None
    protocol_version: str = PROTOCOL_VERSION
    protocol_hash: str = PROTOCOL_HASH

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("unsupported System-1 protocol version")
        if self.protocol_hash != PROTOCOL_HASH:
            raise ValueError("unsupported System-1 protocol hash")
        if self.action_relation is not None and self.action_relation not in ACTION_RELATIONS:
            raise ValueError("invalid action_relation")
        if self.progress_relation is not None and self.progress_relation not in PROGRESS_RELATIONS:
            raise ValueError("invalid progress_relation")
        if self.retry_relation is not None and self.retry_relation not in RETRY_RELATIONS:
            raise ValueError("invalid retry_relation")
        if self.goal_drift is not None and self.goal_drift not in {"yes", "no"}:
            raise ValueError("invalid goal_drift")
        if not isinstance(self.confidence, (int, float)) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        result = {"protocol_version": self.protocol_version,
                  "protocol_hash": self.protocol_hash,
                  "identity": self.identity.to_dict(), "request_id": self.request_id,
                  "sequence": self.sequence, "state_digest": self.state_digest,
                  "policy_hash": self.policy_hash, "backend_id": self.backend_id,
                  "model_id": self.model_id, "calibration_hash": self.calibration_hash,
                  "confidence": float(self.confidence)}
        for key in ("action_relation", "progress_relation", "retry_relation", "goal_drift",
                    "route_preference"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SystemOneJudgment":
        allowed = {"protocol_version", "identity", "request_id", "sequence", "state_digest",
                   "policy_hash", "backend_id", "model_id", "calibration_hash", "confidence",
                   "protocol_hash",
                   "action_relation", "progress_relation", "retry_relation", "goal_drift",
                   "route_preference"}
        required = allowed - {"action_relation", "progress_relation", "retry_relation",
                              "goal_drift", "route_preference"}
        if not isinstance(value, Mapping) or set(value) - allowed or not required.issubset(value):
            raise ValueError("malformed System-1 judgment")
        identity = value.get("identity")
        identity_fields = {"namespace_id", "application_id", "application_instance_id",
                           "agent_id", "agent_instance_id", "session_id", "task_id",
                           "attempt_id", "interaction_id"}
        if not isinstance(identity, Mapping) or set(identity) != identity_fields:
            raise ValueError("malformed System-1 identity")
        return cls(identity=SystemOneIdentity(**dict(identity)),
                   **{key: value[key] for key in allowed - {"identity"} if key in value})


class BehavioralJudge(Protocol):
    """Host-injected internal provider; never registered as a model-facing tool."""
    backend_id: str
    model_id: str
    calibration_hash: str

    def judge(self, frame: SystemOneFrame, *, mode: str) -> Any: ...


__all__ = ["SystemOnePolicy", "SystemOneIdentity", "SystemOneFrame",
           "SystemOneJudgment", "BehavioralJudge"]
