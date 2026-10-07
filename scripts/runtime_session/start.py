"""CFW runtime session startup and resume orchestration."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from runtime_contract.actions import ActionDescriptor, HostExecutionContext
from runtime_contract.telemetry import DurableTelemetryExporter
from runtime_session.host_authority import (
    PolicyRevocationAuthority, SharedWorkflowMembershipAuthority, StateAuthority,
)
from runtime_session.lifecycle import (
    GateDecision,
)
from runtime_session.resolver_bridge import resolve_runtime
from runtime_session.session import RuntimeSession, TelemetrySink
from runtime_session.standalone_route_policy import PolicyEpochRecord
from runtime_start.authorities import bind_start_authorities
from runtime_start.identity import prepare_start_identity
from runtime_start.initialization import initialize_runtime_session
from runtime_start.session_builder import build_runtime_session
from runtime_start.validation import (
    DEFAULT_ACTION_REGISTRY, RUNTIME_MODES, validate_start_contracts,
)

__all__ = ["start", "resume_task", "DEFAULT_ACTION_REGISTRY", "RUNTIME_MODES"]

def start(bundle: resolve_runtime.RuntimeBundle, *,
          telemetry_sink: Optional[TelemetrySink] = None,
          telemetry_exporter: Optional[DurableTelemetryExporter] = None,
          behavioral_judge: Optional[Any] = None,
          state_authority: Optional[StateAuthority] = None,
          policy_revocation_authority: Optional[PolicyRevocationAuthority] = None,
          membership_authority: Optional["SharedWorkflowMembershipAuthority"] = None,
          operator_read_model: Optional[Any] = None,
          runtime_mode: str = "standalone_enforcing",
          telemetry_disabled: bool = False,
          tool_hooks: Optional[List[Callable[[Dict[str, Any]], Optional[GateDecision]]]] = None,
          final_hooks: Optional[List[Callable[[Dict[str, Any]], None]]] = None,
          action_registry: Optional[Mapping[str, ActionDescriptor]] = None,
          host_context: Optional[HostExecutionContext] = None,
          session_nonce: Optional[str] = None,
          session_id: Optional[str] = None,
          policy_epoch_record: Optional[PolicyEpochRecord] = None,
          resume: bool = False) -> RuntimeSession:
    """Inject selected runtime bytes, attach default host telemetry, and
    persist the immutable snapshot. Only tests may explicitly disable the
    sink; ordinary callers cannot silently turn it off."""
    validated = validate_start_contracts(
        bundle, runtime_mode=runtime_mode, behavioral_judge=behavioral_judge,
        policy_epoch_record=policy_epoch_record, action_registry=action_registry)
    runtime_mode = validated.runtime_mode
    resolved_action_registry = validated.action_registry
    supplied_context = host_context or HostExecutionContext()
    identity = prepare_start_identity(
        bundle, supplied_context=supplied_context, session_nonce=session_nonce,
        session_id=session_id, resume=resume)
    session = build_runtime_session(
        bundle, identity=identity,
        action_registry=resolved_action_registry,
        runtime_mode=runtime_mode,
        policy_epoch_record=policy_epoch_record)
    bind_start_authorities(
        session, bundle, runtime_mode=runtime_mode,
        state_authority=state_authority,
        membership_authority=membership_authority,
        policy_revocation_authority=policy_revocation_authority,
        operator_read_model=operator_read_model)
    return initialize_runtime_session(
        session, bundle, identity=identity,
        telemetry_sink=telemetry_sink,
        telemetry_exporter=telemetry_exporter,
        telemetry_disabled=telemetry_disabled,
        tool_hooks=tool_hooks,
        final_hooks=final_hooks,
        policy_epoch_record=policy_epoch_record,
        resume=resume)


def resume_task(bundle: resolve_runtime.RuntimeBundle, session_nonce: str, **kwargs: Any) -> RuntimeSession:
    """Resume a task only from its durable, policy-bound authority journal."""
    if not session_nonce:
        raise ValueError("resume requires the original session nonce")
    return start(bundle, session_nonce=session_nonce, resume=True, **kwargs)
