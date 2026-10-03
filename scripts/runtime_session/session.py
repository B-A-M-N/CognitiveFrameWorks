"""Canonical CFW runtime session state with responsibility mixins."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional

from runtime_contract.actions import ActionDescriptor, ActionObjectiveBinding, HostExecutionContext
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
from runtime_session.action_gate import ActionGateMethods
from runtime_session.attempts import AttemptMethods
from runtime_session.completion import CompletionMethods
from runtime_session.evidence import EvidenceMethods
from runtime_session.external_behavior import ExternalBehaviorMethods
from runtime_session.host_authority import (
    HostIngress, PolicyRevocationAuthority, SharedWorkflowMembershipAuthority,
    StateAuthority, _HostCapability,
)
from runtime_session.lifecycle import (
    ExecutionIdentity, GateDecision, LifecycleController, PolicyEpochRecord,
)
from runtime_session.route_replanning import RouteReplanningMethods
from runtime_session.segments import SessionLifecycleMethods
from runtime_session.standalone_behavior import StandaloneBehaviorMethods

from runtime_session.resolver_bridge import resolve_runtime

TelemetrySink = Callable[[Dict[str, Any]], None]

@dataclass
class RuntimeSession(ExternalBehaviorMethods, StandaloneBehaviorMethods, ActionGateMethods, EvidenceMethods, AttemptMethods, CompletionMethods, RouteReplanningMethods, SessionLifecycleMethods):
    """A started CFW enforcement session: injected components + action hooks."""
    bundle: resolve_runtime.RuntimeBundle
    injected: Dict[str, str] = field(default_factory=dict)
    telemetry: List[TelemetrySink] = field(default_factory=list)
    tool_hooks: List[Callable[[Dict[str, Any]], Optional[GateDecision]]] = field(default_factory=list)
    final_hooks: List[Callable[[Dict[str, Any]], None]] = field(default_factory=list)
    signals: List[Dict[str, Any]] = field(default_factory=list)
    signal_seen: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    statework_states: Dict[tuple[str, str], str] = field(default_factory=dict)
    statework_entry_times: Dict[tuple[str, str], str] = field(default_factory=dict)
    active_statework_ids: set[str] = field(default_factory=set)
    execution_segments: tuple[ExecutionSegment, ...] = ()
    current_segment_index: int = 0
    contradictions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    ledger: EvidenceLedger = field(default_factory=lambda: EvidenceLedger("unbound"))
    _action_registry: Dict[str, ActionDescriptor] = field(default_factory=dict,
                                                          repr=False)
    handoffs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    state_segments: List[StateSegment] = field(default_factory=list)
    _segment_status: Dict[str, SegmentStatus] = field(default_factory=dict,
                                                      repr=False)
    active_lifecycle: set[str] = field(default_factory=set)
    lifecycle_counts: Dict[str, int] = field(default_factory=dict)
    lifecycle: Optional[LifecycleController] = None
    _packet_store: Any = None
    _emit: Optional[TelemetrySink] = None
    _telemetry_exporter: Optional[DurableTelemetryExporter] = None
    state_authority: Optional[StateAuthority] = None
    policy_revocation_authority: Optional[PolicyRevocationAuthority] = None
    membership_authority: Optional[SharedWorkflowMembershipAuthority] = None
    operator_read_model: Optional[Any] = None
    convergence: Optional[ConvergenceController] = None
    termination_disposition: str = "RUNNING"
    termination_permit: Optional[Any] = None
    _operator_sequence: int = 0
    runtime_mode: str = "standalone_enforcing"
    host_context: HostExecutionContext = field(default_factory=HostExecutionContext)
    host_invocations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _consumed_grant_ids: set[str] = field(default_factory=set, repr=False)
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
    shared_workflow_membership: Optional[Mapping[str, Any]] = None
    shared_workflow_memberships: tuple[Mapping[str, Any], ...] = ()
    policy_revocation_revision: int = 0
    revocation_mode: str = "deferred"
    authority_revision: int = 0
    _completion_permit: Optional[CompletionPermit] = field(default=None, repr=False)
    _security_frozen: bool = field(default=False, init=False, repr=False)
    journal_path: Optional[Path] = None
    journal_db_path: Optional[Path] = None
    _host_capability: _HostCapability = field(default_factory=_HostCapability,
                                               init=False, repr=False, compare=False)
    _ledger_token: Any = field(default_factory=object, init=False, repr=False, compare=False)
    route_decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    route_outcomes: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    attempt_descriptors: Dict[str, AttemptDescriptor] = field(default_factory=dict, repr=False)
    attempt_semantics: Dict[str, AttemptSemanticDescriptor] = field(default_factory=dict, repr=False)
    attempt_failures: set[str] = field(default_factory=set, repr=False)
    _semantic_retry_same: set[str] = field(default_factory=set, repr=False)
    shared_branch_ledger: Optional[SharedBranchLedger] = field(default=None, repr=False)
    metacognitive_state: MetacognitiveState = field(
        default_factory=MetacognitiveState, repr=False)
    behavior_authority: Optional[
        StandaloneBehaviorAuthority | ExternalBehaviorAuthority
    ] = field(default=None, repr=False)
    _deferred_semantic_signal_records: tuple[Mapping[str, Any], ...] = field(
        default=(), repr=False)
    semantic_signals_revision: int = field(default=-1, repr=False)
    system_one_sequence: int = 0
    system_one_decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    system_one_outcomes: set[str] = field(default_factory=set, repr=False)
    external_behavior_projection: Dict[str, Any] = field(default_factory=dict, repr=False)
    legacy_external_behavior_state: Dict[str, Any] = field(default_factory=dict, repr=False)
    _external_startup_lifecycle: set[str] = field(default_factory=set, repr=False)
    _external_startup_lifecycle_state: Optional[Mapping[str, Any]] = field(
        default=None, repr=False)
    _last_component_overlay_transition: Optional[PreparedComponentOverlay] = field(
        default=None, repr=False)
    _pending_action_requests: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    _pending_retry_candidates: Dict[str, ActivityFact] = field(default_factory=dict, repr=False)
    _action_objective_bindings: Dict[str, ActionObjectiveBinding] = field(
        default_factory=dict, repr=False)
    policy_epoch_record: Optional[PolicyEpochRecord] = field(default=None, repr=False)
    _system_one_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _system_one_provider_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)

    def _host_ingress(self) -> HostIngress:
        """Return the host-only channel to the adapter implementation.

        It is deliberately private on the agent-facing session.  Host
        integrations should use ``CognitiveRuntime``/their adapter handle;
        exposing a public ``session.host`` property made privileged evidence
        ingestion discoverable to an in-process model/plugin caller.
        """
        return HostIngress(self, self._host_capability)

    @property
    def external_behavior_authority(self) -> bool:
        return isinstance(self.behavior_authority, ExternalBehaviorAuthority)

    def ensure_standalone_behavior_authority(
            self) -> Optional[StandaloneBehaviorAuthority]:
        """Lazily create CFW behavioral services only when CFW owns behavior."""
        authority = self.behavior_authority
        if isinstance(authority, ExternalBehaviorAuthority):
            return None
        if authority is None:
            authority = StandaloneBehaviorAuthority()
            object.__setattr__(self, "behavior_authority", authority)
            if self._deferred_semantic_signal_records:
                authority.semantic_signal_store.restore(
                    self._deferred_semantic_signal_records)
                object.__setattr__(self, "_deferred_semantic_signal_records", ())
        return authority

    def _standalone_authority(self) -> StandaloneBehaviorAuthority:
        authority = self.ensure_standalone_behavior_authority()
        if authority is None:
            raise RuntimeError("standalone behavior service is unavailable under DP authority")
        return authority

    @property
    def system_one_judge(self):
        authority = self.behavior_authority
        return authority.system_one_judge if isinstance(
            authority, StandaloneBehaviorAuthority) else None

    @property
    def system_one_projector(self):
        authority = self.behavior_authority
        return authority.system_one_projector if isinstance(
            authority, StandaloneBehaviorAuthority) else None

    @property
    def metacognitive_controller(self):
        authority = self.behavior_authority
        return authority.metacognitive_controller if isinstance(
            authority, StandaloneBehaviorAuthority) else None

    @property
    def semantic_signals(self):
        authority = self.behavior_authority
        return authority.semantic_signals if isinstance(
            authority, StandaloneBehaviorAuthority) else None

    @semantic_signals.setter
    def semantic_signals(self, value):
        self._standalone_authority().semantic_signals = value

    @property
    def semantic_signal_store(self):
        authority = self.behavior_authority
        return authority.semantic_signal_store if isinstance(
            authority, StandaloneBehaviorAuthority) else None

    @property
    def metacognitive_decision(self):
        authority = self.behavior_authority
        return authority.metacognitive_decision if isinstance(
            authority, StandaloneBehaviorAuthority) else None

    @metacognitive_decision.setter
    def metacognitive_decision(self, value):
        self._standalone_authority().metacognitive_decision = value

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_security_frozen", False) and name in {
                "bundle", "host_context", "_action_registry", "_host_capability",
                "_ledger_token", "_completion_permit", "ledger", "host_invocations",
                "contradictions", "statework_states", "_segment_status",
                "_consumed_grant_ids", "execution_segments", "session_nonce",
                "authority_revision", "session_id", "agent_instance_id", "attempt_id",
                "interaction_id", "parent_session_id", "delegator_agent_id",
                "delegate_agent_id", "delegation_id", "role", "parent_decision_ref",
                "shared_evidence_refs", "delegation_topology", "premise_digest", "failure_attribution",
                "adopted_profile_hashes", "execution_identity", "shared_workflow_membership",
                "shared_workflow_memberships", "policy_revocation_revision", "revocation_mode",
                "policy_revocation_authority", "membership_authority", "operator_read_model",
                "convergence",
                "route_outcomes", "journal_path", "journal_db_path", "_telemetry_exporter", "state_authority",
                "runtime_mode", "attempt_descriptors", "attempt_failures",
                "attempt_semantics", "_semantic_retry_same",
                "shared_branch_ledger", "metacognitive_state", "behavior_authority",
                "semantic_signals_revision",
                "system_one_sequence", "system_one_decisions", "system_one_outcomes",
                "external_behavior_projection", "legacy_external_behavior_state",
                "_external_startup_lifecycle", "_external_startup_lifecycle_state",
                "_last_component_overlay_transition",
        "_pending_action_requests", "_pending_retry_candidates",
        "_action_objective_bindings", "policy_epoch_record"}:
            raise AttributeError(f"runtime security field {name!r} is immutable after start")
        object.__setattr__(self, name, value)











    @property
    def action_registry(self) -> Mapping[str, ActionDescriptor]:
        return MappingProxyType(dict(self._action_registry))

    @property
    def segment_status(self) -> Mapping[str, SegmentStatus]:
        return MappingProxyType(dict(self._segment_status))

    @property
    def completion_permit(self) -> Optional[CompletionPermit]:
        return self._completion_permit
