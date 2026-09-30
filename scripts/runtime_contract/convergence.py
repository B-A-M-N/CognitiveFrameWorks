"""Host-owned convergence ledger.

The convergence controller answers three questions with host state only:

    Did the last attempt produce objective progress?
    Is the next attempt materially different from already-failed attempts?
    Have we exhausted the amount of autonomous recovery justified here?

It never trusts the model's self-report of novelty. Identity is bound to the
execution identity plus a cause, so two agents investigating the same subject
never share a retry budget and never poison each other's context.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

CONVERGENCE_SCHEMA_VERSION = "1.0.0"

# Terminal dispositions. SUCCEEDED keeps the strict completion-permit path.
DISPOSITIONS = {"RUNNING", "SUCCEEDED", "BLOCKED", "FAILED", "HANDOFF"}
TERMINAL_DISPOSITIONS = {"SUCCEEDED", "BLOCKED", "FAILED", "HANDOFF"}

# Gate results returned to the runtime without importing its enum type.
GATE_BLOCK = "block"
GATE_REINSPECT = "require_reinspection"
GATE_CHANGED_HYPOTHESIS = "require_changed_hypothesis"
GATE_ALLOW = None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConvergenceIdentity:
    """Execution-scoped convergence key. Never a workspace or global counter."""

    namespace_id: str
    application_instance_id: str
    agent_instance_id: str
    session_id: str
    task_id: str
    attempt_id: str
    interaction_id: Optional[str]
    cause_id: str

    def __post_init__(self) -> None:
        for name in ("namespace_id", "application_instance_id", "agent_instance_id",
                     "session_id", "task_id", "attempt_id", "cause_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError(f"convergence identity field {name} must be a bounded identifier")

    def key(self) -> str:
        return "|".join((self.namespace_id, self.application_instance_id,
                         self.agent_instance_id, self.session_id, self.task_id,
                         self.attempt_id, self.interaction_id or "", self.cause_id))

    def to_dict(self) -> dict[str, Any]:
        return {"namespace_id": self.namespace_id,
                "application_instance_id": self.application_instance_id,
                "agent_instance_id": self.agent_instance_id,
                "session_id": self.session_id, "task_id": self.task_id,
                "attempt_id": self.attempt_id,
                "interaction_id": self.interaction_id, "cause_id": self.cause_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConvergenceIdentity":
        return cls(namespace_id=str(data["namespace_id"]),
                   application_instance_id=str(data["application_instance_id"]),
                   agent_instance_id=str(data["agent_instance_id"]),
                   session_id=str(data["session_id"]), task_id=str(data["task_id"]),
                   attempt_id=str(data["attempt_id"]),
                   interaction_id=(str(data["interaction_id"])
                                   if data.get("interaction_id") else None),
                   cause_id=str(data["cause_id"]))


@dataclass
class RejectedPath:
    """Bounded negative memory for one disproved investigative branch."""

    cause_id: str
    action_fingerprint: str
    hypothesis_fingerprint: str
    premise_digest: Optional[str]
    rejecting_evidence_ref: Optional[str]
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"cause_id": self.cause_id, "action_fingerprint": self.action_fingerprint,
                "hypothesis_fingerprint": self.hypothesis_fingerprint,
                "premise_digest": self.premise_digest,
                "rejecting_evidence_ref": self.rejecting_evidence_ref,
                "recorded_at": self.recorded_at}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RejectedPath":
        return cls(cause_id=str(data["cause_id"]),
                   action_fingerprint=str(data["action_fingerprint"]),
                   hypothesis_fingerprint=str(data["hypothesis_fingerprint"]),
                   premise_digest=(str(data["premise_digest"])
                                   if data.get("premise_digest") else None),
                   rejecting_evidence_ref=(str(data["rejecting_evidence_ref"])
                                           if data.get("rejecting_evidence_ref") else None),
                   recorded_at=str(data.get("recorded_at") or ""))


@dataclass
class RetryState:
    """Host-owned retry/recovery accounting for exactly one cause."""

    identity: ConvergenceIdentity
    action_fingerprint: str = ""
    hypothesis_fingerprint: str = ""
    premise_digest: Optional[str] = None
    same_approach_retries: int = 0
    recovery_epochs: int = 0
    no_progress_epochs: int = 0
    last_failure_evidence_ref: Optional[str] = None
    last_reinspection_revision: int = 0
    last_progress_digest: Optional[str] = None
    last_failure_digest: Optional[str] = None
    disposition: str = "RUNNING"
    terminated_reason: Optional[str] = None
    rejected_paths: list[RejectedPath] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONVERGENCE_SCHEMA_VERSION,
            "identity": self.identity.to_dict(),
            "action_fingerprint": self.action_fingerprint,
            "hypothesis_fingerprint": self.hypothesis_fingerprint,
            "premise_digest": self.premise_digest,
            "same_approach_retries": self.same_approach_retries,
            "recovery_epochs": self.recovery_epochs,
            "no_progress_epochs": self.no_progress_epochs,
            "last_failure_evidence_ref": self.last_failure_evidence_ref,
            "last_reinspection_revision": self.last_reinspection_revision,
            "last_progress_digest": self.last_progress_digest,
            "last_failure_digest": self.last_failure_digest,
            "disposition": self.disposition,
            "terminated_reason": self.terminated_reason,
            "rejected_paths": [item.to_dict() for item in self.rejected_paths],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RetryState":
        state = cls(identity=ConvergenceIdentity.from_dict(data["identity"]))
        state.action_fingerprint = str(data.get("action_fingerprint") or "")
        state.hypothesis_fingerprint = str(data.get("hypothesis_fingerprint") or "")
        state.premise_digest = (str(data["premise_digest"])
                               if data.get("premise_digest") else None)
        state.same_approach_retries = int(data.get("same_approach_retries", 0) or 0)
        state.recovery_epochs = int(data.get("recovery_epochs", 0) or 0)
        state.no_progress_epochs = int(data.get("no_progress_epochs", 0) or 0)
        state.last_failure_evidence_ref = (str(data["last_failure_evidence_ref"])
                                           if data.get("last_failure_evidence_ref") else None)
        state.last_reinspection_revision = int(data.get("last_reinspection_revision", 0) or 0)
        state.last_progress_digest = (str(data["last_progress_digest"])
                                      if data.get("last_progress_digest") else None)
        state.last_failure_digest = (str(data["last_failure_digest"])
                                     if data.get("last_failure_digest") else None)
        state.disposition = str(data.get("disposition") or "RUNNING")
        state.terminated_reason = (str(data["terminated_reason"])
                                   if data.get("terminated_reason") else None)
        state.rejected_paths = [RejectedPath.from_dict(item)
                               for item in data.get("rejected_paths") or ()]
        return state

    def compact_state_block(self, *, max_recovery_epochs_without_progress: int) -> str:
        """Small host-generated negative memory. Never a replayed checkpoint."""
        if not self.rejected_paths:
            return ""
        lines = ["FAILED PATHS (evidence only):"]
        for path in self.rejected_paths[-5:]:
            # Report the investigative RESULT, never another agent's
            # reasoning. Narrative transfer is how context poisoning spreads.
            proof = (f"disproved by evidence {path.rejecting_evidence_ref}"
                     if path.rejecting_evidence_ref else "no fresh evidence")
            lines.append(f"- cause {path.cause_id} / approach {path.action_fingerprint[:8]}: {proof}")
        lines.append(f"Current recovery epoch: {self.recovery_epochs}/{max_recovery_epochs_without_progress}")
        lines.append("Required novelty: new causal hypothesis backed by fresh evidence")
        return "\n".join(lines)


class ConvergenceController:
    """Owns convergence state for one session, keyed by execution identity."""

    def __init__(self, *, identity: Mapping[str, Any],
                 max_recovery_epochs_without_progress: int = 3,
                 max_same_approach_retries: int = 2) -> None:
        if max_recovery_epochs_without_progress < 1:
            raise ValueError("max_recovery_epochs_without_progress must be positive")
        if max_same_approach_retries < 1:
            raise ValueError("max_same_approach_retries must be positive")
        self._identity = dict(identity)
        self.max_recovery_epochs_without_progress = int(max_recovery_epochs_without_progress)
        self.max_same_approach_retries = int(max_same_approach_retries)
        self._states: dict[str, RetryState] = {}

    def identity_for(self, cause_id: str) -> ConvergenceIdentity:
        return ConvergenceIdentity(
            namespace_id=str(self._identity.get("namespace_id") or "default"),
            application_instance_id=str(self._identity.get("application_instance_id") or "unknown-application"),
            agent_instance_id=str(self._identity.get("agent_instance_id") or "agent"),
            session_id=str(self._identity.get("session_id") or "session"),
            task_id=str(self._identity.get("task_id") or "task"),
            attempt_id=str(self._identity.get("attempt_id") or "attempt-1"),
            interaction_id=(str(self._identity["interaction_id"])
                            if self._identity.get("interaction_id") else None),
            cause_id=str(cause_id))

    def state(self, cause_id: str) -> RetryState:
        identity = self.identity_for(cause_id)
        return self._states.setdefault(identity.key(), RetryState(identity=identity))

    def states(self) -> list[RetryState]:
        return [self._states[key] for key in sorted(self._states)]

    def all_terminated(self) -> bool:
        return bool(self._states) and all(
            state.disposition in TERMINAL_DISPOSITIONS for state in self._states.values())

    def dispositions(self) -> dict[str, str]:
        return {key: state.disposition for key, state in sorted(self._states.items())}

    def record_failure(self, *, cause_id: str, action_fingerprint: str,
                       hypothesis_fingerprint: str, evidence_ref: str | None = None,
                       premise_digest: str | None = None,
                       observed_at: str | None = None) -> RetryState:
        state = self.state(cause_id)
        state.last_failure_evidence_ref = evidence_ref
        state.last_failure_digest = _digest(
            {"action": action_fingerprint, "hypothesis": hypothesis_fingerprint,
             "evidence": evidence_ref, "premise": premise_digest})
        if (state.action_fingerprint == action_fingerprint
                and state.hypothesis_fingerprint == hypothesis_fingerprint
                and state.premise_digest == premise_digest):
            state.same_approach_retries += 1
        else:
            state.same_approach_retries = 1
        state.action_fingerprint = action_fingerprint
        state.hypothesis_fingerprint = hypothesis_fingerprint
        state.premise_digest = premise_digest
        return state

    def record_reinspection(self, *, cause_id: str, revision: int) -> RetryState:
        state = self.state(cause_id)
        state.last_reinspection_revision = max(state.last_reinspection_revision, int(revision))
        return state

    def record_progress(self, *, cause_id: str, progress_digest: str,
                        evidence_ref: str | None = None,
                        observed_at: str | None = None) -> RetryState:
        """Only host-observable progress resets the no-progress budget."""
        state = self.state(cause_id)
        state.last_progress_digest = progress_digest
        state.recovery_epochs = 0
        state.no_progress_epochs = 0
        state.same_approach_retries = 0
        if evidence_ref:
            state.last_failure_evidence_ref = None
        return state

    def record_recovery_epoch(self, *, cause_id: str) -> RetryState:
        """A recovery hop with no intervening host-observed progress."""
        state = self.state(cause_id)
        state.recovery_epochs += 1
        if state.last_progress_digest != state.last_failure_digest:
            state.no_progress_epochs += 1
        return state

    def reject_path(self, *, cause_id: str, action_fingerprint: str,
                    hypothesis_fingerprint: str, evidence_ref: str | None,
                    premise_digest: str | None, observed_at: str) -> RetryState:
        state = self.state(cause_id)
        state.rejected_paths.append(RejectedPath(
            cause_id=cause_id, action_fingerprint=action_fingerprint,
            hypothesis_fingerprint=hypothesis_fingerprint,
            premise_digest=premise_digest, rejecting_evidence_ref=evidence_ref,
            recorded_at=observed_at))
        del state.rejected_paths[:-32]
        return state

    def branch_key(self, *, cause_id: str, hypothesis_fingerprint: str,
                    premise_digest: str | None) -> str:
        """Same cause + same premise is the same investigative branch.

        This is what stops agent fan-out from bypassing retry limits: an
        inherited premise is not independent work.
        """
        return "|".join((cause_id, premise_digest or "", hypothesis_fingerprint))

    def evaluate_retry(self, *, cause_id: str, action_fingerprint: str,
                       hypothesis_fingerprint: str, premise_digest: str | None = None,
                       failure_digest: str | None = None,
                       delegated: bool = False) -> str:
        """Decide one retry. Novelty is judged against host state, not claims."""
        state = self.state(cause_id)
        if delegated and state.action_fingerprint == action_fingerprint \
                and state.hypothesis_fingerprint == hypothesis_fingerprint \
                and state.premise_digest == premise_digest:
            # Delegated work that repeats this branch is not an independent
            # recovery attempt.
            state.same_approach_retries += 1
        if state.disposition in TERMINAL_DISPOSITIONS:
            # A terminated cause cannot be revived by another retry.
            return GATE_BLOCK
        if state.no_progress_epochs >= self.max_recovery_epochs_without_progress:
            state.disposition = "BLOCKED"
            state.terminated_reason = "max_recovery_epochs_without_progress"
            return GATE_BLOCK
        unchanged = (state.action_fingerprint == action_fingerprint
                     and state.hypothesis_fingerprint == hypothesis_fingerprint
                     and state.premise_digest == premise_digest)
        if unchanged:
            # Repeated identical attempts are exactly what the budget bounds.
            if state.last_reinspection_revision:
                return GATE_CHANGED_HYPOTHESIS
            if state.same_approach_retries >= self.max_same_approach_retries:
                return GATE_CHANGED_HYPOTHESIS
            return GATE_REINSPECT
        if (failure_digest is not None and state.last_progress_digest
                and state.last_progress_digest == state.last_failure_digest):
            # No objective progress since the last recorded failure.
            if state.last_reinspection_revision:
                return GATE_CHANGED_HYPOTHESIS
            return GATE_REINSPECT
        if state.same_approach_retries >= self.max_same_approach_retries:
            return GATE_CHANGED_HYPOTHESIS
        return GATE_ALLOW

    def terminate(self, *, disposition: str, reason: str) -> None:
        if disposition not in TERMINAL_DISPOSITIONS:
            raise ValueError("termination disposition is not terminal")
        for state in self._states.values():
            state.disposition = disposition
            state.terminated_reason = reason

    def export(self) -> dict[str, Any]:
        return {"schema_version": CONVERGENCE_SCHEMA_VERSION,
                "max_recovery_epochs_without_progress": self.max_recovery_epochs_without_progress,
                "max_same_approach_retries": self.max_same_approach_retries,
                "states": {key: state.to_dict() for key, state in sorted(self._states.items())}}

    def restore(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            return
        if payload.get("schema_version") != CONVERGENCE_SCHEMA_VERSION:
            return
        self.max_recovery_epochs_without_progress = int(
            payload.get("max_recovery_epochs_without_progress") or
            self.max_recovery_epochs_without_progress)
        self.max_same_approach_retries = int(
            payload.get("max_same_approach_retries") or self.max_same_approach_retries)
        for key, data in (payload.get("states") or {}).items():
            try:
                self._states[str(key)] = RetryState.from_dict(data)
            except (KeyError, TypeError, ValueError):
                continue
