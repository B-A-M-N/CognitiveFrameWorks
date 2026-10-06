"""Cohesive mutable state owned by CFW runtime subsystems.

The session coordinates these aggregates; it does not own a flat collection of
every subsystem's mutable fields.  Cross-subsystem access is intentionally
spelled through the owning aggregate (for example, ``session.evidence`` or
``session.identity_authority.authorities``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional

from runtime_contract.actions import (
    ActionDescriptor, ActionObjectiveBinding, HostExecutionContext,
)
from runtime_contract.component_control import PreparedComponentOverlay
from runtime_contract.convergence import (
    AttemptDescriptor, ConvergenceController, SharedBranchLedger,
)
from runtime_contract.evidence import CompletionPermit, EvidenceLedger, StateSegment
from runtime_contract.metacognition import MetacognitiveState
from runtime_contract.stateworks import ExecutionSegment, SegmentStatus
from runtime_contract.system_one import ActivityFact, AttemptSemanticDescriptor
from runtime_session.behavior_authority import (
    ExternalBehaviorAuthority, StandaloneBehaviorAuthority,
)
from runtime_contract.metacognition import MetaDecision, SemanticSignals
from runtime_session.host_authority import (
    PolicyRevocationAuthority, SharedWorkflowMembershipAuthority, StateAuthority,
    _HostCapability,
)
from runtime_session.lifecycle import (
    ExecutionIdentity, LifecycleController,
)
from runtime_session.standalone_route_policy import StandaloneRoutePolicy

TelemetrySink = Callable[[Dict[str, Any]], None]


@dataclass(frozen=True)
class SessionIdentity:
    """Immutable execution identity, delegation context, and host context."""

    session_nonce: str = ""
    session_id: str = ""
    agent_instance_id: str = "agent"
    attempt_id: str = "attempt-1"
    interaction_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    delegator_agent_id: Optional[str] = None
    delegate_agent_id: Optional[str] = None
    delegation_id: Optional[str] = None
    role: Optional[str] = None
    parent_decision_ref: Optional[str] = None
    shared_evidence_refs: tuple[str, ...] = ()
    delegation_topology: Optional[str] = None
    premise_digest: Optional[str] = None
    failure_attribution: str = "none"
    adopted_profile_hashes: tuple[str, ...] = ()
    execution_identity: Optional[ExecutionIdentity] = None
    host_context: HostExecutionContext = field(default_factory=HostExecutionContext)

    def __post_init__(self) -> None:
        object.__setattr__(self, "shared_evidence_refs",
                           tuple(self.shared_evidence_refs))
        object.__setattr__(self, "adopted_profile_hashes",
                           tuple(self.adopted_profile_hashes))


@dataclass(frozen=True)
class AuthorityBindings:
    """Authorities pinned by the host for the lifetime of a started session."""

    state_authority: Optional[StateAuthority] = None
    workflow_port: Optional[Any] = None
    policy_revocation_authority: Optional[PolicyRevocationAuthority] = None
    membership_authority: Optional[SharedWorkflowMembershipAuthority] = None
    operator_read_model: Optional[Any] = None
    runtime_mode: str = "standalone_enforcing"
    shared_workflow_membership: Optional[Mapping[str, Any]] = None
    shared_workflow_memberships: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.shared_workflow_membership is not None:
            object.__setattr__(self, "shared_workflow_membership",
                               MappingProxyType(dict(self.shared_workflow_membership)))
        object.__setattr__(self, "shared_workflow_memberships", tuple(
            MappingProxyType(dict(item)) for item in self.shared_workflow_memberships))


@dataclass
class AuthorityRevisionState:
    """Monotonic host authority revisions with explicit mutation methods."""

    _authority_revision: int = 0
    _policy_revocation_revision: int = 0
    _revocation_mode: str = "deferred"

    @property
    def authority_revision(self) -> int:
        return self._authority_revision

    @property
    def policy_revocation_revision(self) -> int:
        return self._policy_revocation_revision

    @property
    def revocation_mode(self) -> str:
        return self._revocation_mode

    def observe_authority(self, revision: int) -> int:
        object.__setattr__(
            self, "_authority_revision",
            max(self._authority_revision, int(revision)))
        return self._authority_revision

    def observe_policy_revocation(self, revision: int, mode: Optional[str] = None) -> int:
        object.__setattr__(
            self, "_policy_revocation_revision",
            max(self._policy_revocation_revision, int(revision)))
        if mode:
            object.__setattr__(self, "_revocation_mode", str(mode))
        return self._policy_revocation_revision

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_authority_revision", "_policy_revocation_revision",
                    "_revocation_mode"} and name in self.__dict__:
            raise AttributeError("authority revisions change through observe methods")
        object.__setattr__(self, name, value)


@dataclass(frozen=True)
class IdentityAuthorityState:
    """Frozen identity and host bindings, plus a narrow revision mutator."""

    identity: SessionIdentity
    authorities: AuthorityBindings = field(default_factory=AuthorityBindings)
    _host_capability: _HostCapability = field(default_factory=_HostCapability,
                                                repr=False, compare=False)
    revisions: AuthorityRevisionState = field(default_factory=AuthorityRevisionState,
                                               compare=False)


@dataclass(frozen=True)
class WorkflowExecutionPlan:
    """Immutable host-resolved execution segments for one task."""

    execution_segments: tuple[ExecutionSegment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_segments", tuple(self.execution_segments))


@dataclass
class LifecycleWorkflowState:
    """Lifecycle controller and the StateWork/workflow mirror it maintains."""

    statework_states: Dict[tuple[str, str], str] = field(default_factory=dict)
    statework_entry_times: Dict[tuple[str, str], str] = field(default_factory=dict)
    active_statework_ids: set[str] = field(default_factory=set)
    _plan: WorkflowExecutionPlan = field(default_factory=WorkflowExecutionPlan,
                                          repr=False)
    current_segment_index: int = 0
    handoffs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _statuses: Dict[str, SegmentStatus] = field(default_factory=dict, repr=False)
    active_lifecycle: set[str] = field(default_factory=set)
    lifecycle_counts: Dict[str, int] = field(default_factory=dict)
    _lifecycle: Optional[LifecycleController] = field(default=None, repr=False)
    packet_store: Any = None
    termination_disposition: str = "RUNNING"
    termination_permit: Optional[Any] = None

    @property
    def current_segment(self) -> ExecutionSegment:
        index = min(self.current_segment_index, len(self.execution_segments) - 1)
        return self.execution_segments[index]

    @property
    def execution_segments(self) -> tuple[ExecutionSegment, ...]:
        return self._plan.execution_segments

    @property
    def plan(self) -> WorkflowExecutionPlan:
        return self._plan

    @property
    def lifecycle(self) -> Optional[LifecycleController]:
        return self._lifecycle

    def bind_lifecycle(self, controller: LifecycleController) -> None:
        if self._lifecycle is not None:
            raise RuntimeError("lifecycle controller is already bound")
        object.__setattr__(self, "_lifecycle", controller)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_plan", "_lifecycle"} and name in self.__dict__:
            raise AttributeError(f"workflow binding {name[1:]} is immutable")
        object.__setattr__(self, name, value)

    @property
    def segment_status(self) -> Mapping[str, SegmentStatus]:
        return MappingProxyType(dict(self._statuses))

    @property
    def statuses(self) -> Mapping[str, SegmentStatus]:
        return self.segment_status

    def set_segment_status(self, segment_id: str, status: SegmentStatus) -> None:
        self._statuses[segment_id] = status

    def sync_lifecycle_projection(self) -> None:
        if self.lifecycle is not None:
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)


@dataclass
class ActionState:
    """Action capabilities, route outcomes, invocations, and pending requests."""

    _registry: Mapping[str, ActionDescriptor] = field(default_factory=dict, repr=False)
    host_invocations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _consumed_grant_ids: set[str] = field(default_factory=set, repr=False)
    route_decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict,
                                                     repr=False)
    route_outcomes: Dict[str, Dict[str, Any]] = field(default_factory=dict,
                                                     repr=False)
    _pending_action_requests: Dict[str, Dict[str, Any]] = field(default_factory=dict,
                                                                repr=False)
    _objective_bindings: Dict[str, ActionObjectiveBinding] = field(default_factory=dict,
                                                                   repr=False)

    @property
    def action_registry(self) -> Mapping[str, ActionDescriptor]:
        return self._registry

    @property
    def pending_action_requests(self) -> Mapping[str, Dict[str, Any]]:
        return MappingProxyType({key: dict(value)
                                 for key, value in self._pending_action_requests.items()})

    @property
    def objective_bindings(self) -> Mapping[str, ActionObjectiveBinding]:
        return MappingProxyType(dict(self._objective_bindings))

    @property
    def consumed_grant_ids(self) -> frozenset[str]:
        return frozenset(self._consumed_grant_ids)

    def consume_grant(self, grant_id: str) -> None:
        self._consumed_grant_ids.add(grant_id)

    def bind_registry(self, registry: Mapping[str, ActionDescriptor]) -> None:
        if self._registry:
            raise RuntimeError("action registry is already bound")
        object.__setattr__(self, "_registry", MappingProxyType(dict(registry)))

    def add_objective_binding(self, binding: ActionObjectiveBinding,
                              *, max_bindings: int = 512) -> None:
        self._objective_bindings[binding.binding_digest] = binding
        while len(self._objective_bindings) > max_bindings:
            self._objective_bindings.pop(next(iter(self._objective_bindings)))

    def take_objective_binding(self, digest: str) -> Optional[ActionObjectiveBinding]:
        return self._objective_bindings.pop(digest, None)

    def put_pending_request(self, request_id: str, request: Dict[str, Any]) -> None:
        self._pending_action_requests[request_id] = request

    def update_pending_request(self, request_id: str, **updates: Any) -> None:
        request = self._pending_action_requests.get(request_id)
        if request is not None:
            request.update(updates)

    def take_pending_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self._pending_action_requests.pop(request_id, None)

    def clear_pending(self) -> None:
        self._pending_action_requests.clear()
        self._objective_bindings.clear()

    def __post_init__(self) -> None:
        object.__setattr__(self, "_registry",
                           MappingProxyType(dict(self._registry)))

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_registry" and name in self.__dict__:
            raise AttributeError("action registry binding is immutable")
        object.__setattr__(self, name, value)


@dataclass
class EvidenceState:
    """Evidence ledger, host attestations, contradictions, and completion proofs."""

    ledger: EvidenceLedger = field(default_factory=lambda: EvidenceLedger("unbound"))
    _writer_token: Any = field(default_factory=object, repr=False, compare=False)
    contradictions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    state_segments: List[StateSegment] = field(default_factory=list)
    _completion_permit: Optional[CompletionPermit] = field(default=None, repr=False)

    @property
    def completion_permit(self) -> Optional[CompletionPermit]:
        return self._completion_permit

    def issue_completion_permit(self, permit: CompletionPermit) -> None:
        object.__setattr__(self, "_completion_permit", permit)

    def revoke_completion_permit(self) -> None:
        object.__setattr__(self, "_completion_permit", None)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"ledger", "_writer_token", "_completion_permit"} \
                and name in self.__dict__:
            raise AttributeError(f"evidence binding {name.lstrip('_')} is immutable")
        object.__setattr__(self, name, value)


@dataclass
class AttemptConvergenceState:
    """Attempt evidence and convergence decisions for bounded recovery."""

    _controller: Optional[ConvergenceController] = field(default=None, repr=False)
    descriptors: Dict[str, AttemptDescriptor] = field(default_factory=dict, repr=False)
    semantics: Dict[str, AttemptSemanticDescriptor] = field(default_factory=dict,
                                                              repr=False)
    failures: set[str] = field(default_factory=set, repr=False)
    _semantic_retry_same: set[str] = field(default_factory=set, repr=False)
    _shared_branch_ledger: Optional[SharedBranchLedger] = field(default=None,
                                                                repr=False)
    _pending_retry_candidates: Dict[str, ActivityFact] = field(default_factory=dict,
                                                                 repr=False)

    @property
    def controller(self) -> Optional[ConvergenceController]:
        return self._controller

    @property
    def shared_branch_ledger(self) -> Optional[SharedBranchLedger]:
        return self._shared_branch_ledger

    @property
    def semantic_retry_same(self) -> frozenset[str]:
        return frozenset(self._semantic_retry_same)

    @property
    def pending_retry_candidates(self) -> Mapping[str, ActivityFact]:
        return MappingProxyType(dict(self._pending_retry_candidates))

    def bind_shared_branch_ledger(self, ledger: SharedBranchLedger) -> None:
        if self._shared_branch_ledger is not None:
            raise RuntimeError("shared branch ledger is already bound")
        object.__setattr__(self, "_shared_branch_ledger", ledger)

    def clear_semantic_retry(self, attempt_ref: str) -> None:
        self._semantic_retry_same.discard(attempt_ref)

    def mark_semantic_retry(self, attempt_ref: str) -> None:
        self._semantic_retry_same.add(attempt_ref)

    def record_pending_retry(self, attempt_ref: str, candidate: ActivityFact) -> None:
        self._pending_retry_candidates[attempt_ref] = candidate

    def take_pending_retry(self, attempt_ref: str) -> Optional[ActivityFact]:
        return self._pending_retry_candidates.pop(attempt_ref, None)

    def clear_pending_retries(self) -> None:
        self._pending_retry_candidates.clear()

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_controller", "_shared_branch_ledger"} \
                and name in self.__dict__:
            raise AttributeError(f"attempt binding {name[1:]} is immutable")
        object.__setattr__(self, name, value)


@dataclass
class BehaviorState:
    """Behavior authority, decision history, signal journal, and projections."""

    _authority: Optional[StandaloneBehaviorAuthority | ExternalBehaviorAuthority] = field(
        default=None, repr=False)
    signals: List[Dict[str, Any]] = field(default_factory=list)
    signal_seen: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    signal_journal: List[tuple[int, Dict[str, Any]]] = field(default_factory=list,
                           repr=False)
    signal_cursor: int = 0
    signal_ack_cursor: int = 0
    signal_lock: Any = field(default_factory=threading.RLock, repr=False)
    deferred_semantic_signal_records: tuple[Mapping[str, Any], ...] = field(
        default=(), repr=False)
    semantic_signals_revision: int = field(default=-1, repr=False)
    system_one_sequence: int = 0
    system_one_decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict,
                                                              repr=False)
    system_one_outcomes: set[str] = field(default_factory=set, repr=False)
    external_projection: Dict[str, Any] = field(default_factory=dict, repr=False)
    external_startup_lifecycle: set[str] = field(default_factory=set, repr=False)
    external_startup_lifecycle_state: Optional[Mapping[str, Any]] = field(
        default=None, repr=False)
    last_component_overlay_transition: Optional[PreparedComponentOverlay] = field(
        default=None, repr=False)
    metacognitive: MetacognitiveState = field(default_factory=MetacognitiveState,
                                               repr=False)
    system_one_lock: Any = field(default_factory=threading.RLock, repr=False)
    system_one_provider_lock: Any = field(default_factory=threading.RLock, repr=False)

    @property
    def is_external(self) -> bool:
        return isinstance(self.authority, ExternalBehaviorAuthority)

    @property
    def authority(self) -> Optional[StandaloneBehaviorAuthority | ExternalBehaviorAuthority]:
        return self._authority

    @property
    def standalone_route_policy(self) -> Optional[StandaloneRoutePolicy]:
        authority = self.authority
        return authority.route_policy if isinstance(
            authority, StandaloneBehaviorAuthority) else None

    def bind_standalone_authority(self,
                                 authority: StandaloneBehaviorAuthority) -> None:
        if self._authority is not None and self._authority is not authority:
            raise RuntimeError("behavior authority is already bound")
        if not isinstance(authority, StandaloneBehaviorAuthority):
            raise TypeError("standalone behavior authority has an invalid type")
        object.__setattr__(self, "_authority", authority)

    @property
    def system_one_judge(self) -> Any:
        return (self.authority.system_one_judge
                if isinstance(self.authority, StandaloneBehaviorAuthority) else None)

    @property
    def system_one_projector(self) -> Any:
        return (self.authority.system_one_projector
                if isinstance(self.authority, StandaloneBehaviorAuthority) else None)

    @property
    def metacognitive_controller(self) -> Any:
        return (self.authority.metacognitive_controller
                if isinstance(self.authority, StandaloneBehaviorAuthority) else None)

    @property
    def semantic_signals(self) -> Optional[SemanticSignals]:
        return (self.authority.semantic_signals
                if isinstance(self.authority, StandaloneBehaviorAuthority) else None)

    def set_semantic_signals(self, signals: SemanticSignals) -> None:
        if not isinstance(self.authority, StandaloneBehaviorAuthority):
            raise RuntimeError("CFW does not own semantic signals under external authority")
        self.authority.semantic_signals = signals

    @property
    def semantic_signal_store(self) -> Any:
        return (self.authority.semantic_signal_store
                if isinstance(self.authority, StandaloneBehaviorAuthority) else None)

    @property
    def metacognitive_decision(self) -> Optional[MetaDecision]:
        return (self.authority.metacognitive_decision
                if isinstance(self.authority, StandaloneBehaviorAuthority) else None)

    def set_metacognitive_decision(self, decision: MetaDecision) -> None:
        if not isinstance(self.authority, StandaloneBehaviorAuthority):
            raise RuntimeError("CFW does not own metacognition under external authority")
        self.authority.metacognitive_decision = decision

    def bind_external_authority(self) -> None:
        """Bind the host-selected external behavior owner before first use."""
        if self.authority is not None and not self.is_external:
            raise RuntimeError("standalone behavior was already initialized")
        object.__setattr__(self, "_authority", ExternalBehaviorAuthority())

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_authority" and name in self.__dict__:
            raise AttributeError("behavior authority is bound through its owner")
        object.__setattr__(self, name, value)


@dataclass
class TelemetryJournalState:
    """Output sinks and durable journal coordinates for one runtime session."""

    _sinks: List[TelemetrySink] = field(default_factory=list, repr=False)
    _emit: Optional[TelemetrySink] = field(default=None, repr=False)
    _emitter_bound: bool = field(default=False, init=False, repr=False)
    _exporter: Any = field(default=None, repr=False)
    _journal_path: Optional[Path] = field(default=None, repr=False)
    _journal_db_path: Optional[Path] = field(default=None, repr=False)
    _operator_sequence: int = field(default=0, repr=False)

    @property
    def sinks(self) -> tuple[TelemetrySink, ...]:
        return tuple(self._sinks)

    @property
    def emit(self) -> Optional[TelemetrySink]:
        return self._emit

    @property
    def exporter(self) -> Any:
        return self._exporter

    @property
    def journal_path(self) -> Optional[Path]:
        return self._journal_path

    @property
    def journal_db_path(self) -> Optional[Path]:
        return self._journal_db_path

    @property
    def operator_sequence(self) -> int:
        return self._operator_sequence

    def bind_journal(self, path: Path, db_path: Path) -> None:
        if self._journal_path is not None or self._journal_db_path is not None:
            raise RuntimeError("telemetry journal paths are already bound")
        object.__setattr__(self, "_journal_path", path)
        object.__setattr__(self, "_journal_db_path", db_path)

    def bind_emitter(self, emitter: Optional[TelemetrySink]) -> None:
        if self._emitter_bound:
            raise RuntimeError("telemetry emitter is already bound")
        object.__setattr__(self, "_emit", emitter)
        object.__setattr__(self, "_emitter_bound", True)

    def bind_exporter(self, exporter: Any) -> None:
        if self._exporter is not None and self._exporter is not exporter:
            raise RuntimeError("telemetry exporter is already bound")
        object.__setattr__(self, "_exporter", exporter)

    def allocate_operator_sequence(self, read_model: Any) -> int:
        sequence = int(read_model.allocate_sequence())
        object.__setattr__(self, "_operator_sequence", sequence)
        return sequence

    def __setattr__(self, name: str, value: Any) -> None:
        immutable = {"_sinks", "_emit", "_emitter_bound", "_exporter", "_journal_path",
                     "_journal_db_path", "_operator_sequence"}
        if name in immutable and name in self.__dict__:
            raise AttributeError(f"telemetry binding {name[1:]} changes through its owner")
        object.__setattr__(self, name, value)
