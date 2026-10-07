"""Runtime session coordinator over cohesive subsystem state aggregates."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from runtime_session.action_gate import ActionGateMethods
from runtime_session.attempts import AttemptMethods
from runtime_session.completion import CompletionMethods
from runtime_session.evidence import EvidenceMethods
from runtime_session.external_behavior import ExternalBehaviorMethods
from runtime_session.host_authority import HostIngress
from runtime_session.lifecycle import GateDecision
from runtime_session.resolver_bridge import resolve_runtime
from runtime_session.route_replanning import RouteReplanningMethods
from runtime_session.segments import SessionLifecycleMethods
from runtime_session.standalone_behavior import StandaloneBehaviorMethods
from runtime_session.state import (
    ActionState, AttemptConvergenceState, BehaviorState, EvidenceState,
    IdentityAuthorityState, LifecycleWorkflowState, TelemetryJournalState,
    TelemetrySink,
)


@dataclass
class RuntimeSession(ExternalBehaviorMethods, StandaloneBehaviorMethods,
                     ActionGateMethods, EvidenceMethods, AttemptMethods,
                     CompletionMethods, RouteReplanningMethods,
                     SessionLifecycleMethods):
    """A started CFW session coordinating independent subsystem aggregates."""

    bundle: resolve_runtime.RuntimeBundle
    identity_authority: IdentityAuthorityState
    injected: Dict[str, str] = field(default_factory=dict)
    tool_hooks: List[Callable[[Dict[str, Any]], Optional[GateDecision]]] = field(
        default_factory=list)
    final_hooks: List[Callable[[Dict[str, Any]], None]] = field(default_factory=list)
    lifecycle_workflow: LifecycleWorkflowState = field(
        default_factory=LifecycleWorkflowState)
    actions: ActionState = field(default_factory=ActionState)
    evidence: EvidenceState = field(default_factory=EvidenceState)
    attempts: AttemptConvergenceState = field(default_factory=AttemptConvergenceState)
    behavior: BehaviorState = field(default_factory=BehaviorState)
    telemetry_state: TelemetryJournalState = field(default_factory=TelemetryJournalState)
    _security_frozen: bool = field(default=False, init=False, repr=False)

    def _host_ingress(self) -> HostIngress:
        """Return the host-only channel to the adapter implementation.

        It remains private on the agent-facing session; integrations use the
        host adapter handle, which carries its own capability boundary.
        """
        return HostIngress(self, self.identity_authority._host_capability)

    def ensure_standalone_behavior_authority(self):
        """Lazily create CFW behavior services only when CFW owns behavior."""
        from runtime_session.behavior_authority import StandaloneBehaviorAuthority

        authority = self.behavior.authority
        if self.behavior.is_external:
            return None
        if authority is None:
            authority = StandaloneBehaviorAuthority()
            self.behavior.bind_standalone_authority(authority)
        if isinstance(authority, StandaloneBehaviorAuthority):
            authority.activated = True
        return authority

    def seal(self) -> None:
        """Seal coordinator bindings after startup; aggregate owners keep mutating."""
        object.__setattr__(self, "_security_frozen", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_security_frozen", False):
            raise AttributeError(
                "runtime session coordinator is sealed; mutate its owning subsystem state")
        object.__setattr__(self, name, value)
