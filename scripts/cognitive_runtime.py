"""Compatibility entry point for the decomposed CFW runtime host API."""
from __future__ import annotations

# Keep the original public import surface stable while responsibilities live
# under runtime_host.
from runtime import (CompletionClaim, GateDecision, RuntimeSession,
                     SharedWorkflowMembershipAuthority, start)
from runtime_contract.actions import (ActionDescriptor, HostExecutionContext,
                                       effective_action_registry)
from runtime_contract.advisor import BehavioralAdvisor
from resolve_runtime import RuntimeBundle, TaskRequest, resolve
from runtime_host.session_registry import HOST_SESSION_REGISTRY as _HOST_SESSION_REGISTRY
from runtime_host.handles import AgentSessionHandle, HostSessionHandle, RuntimeTaskHandle
from runtime_host.host_api import (CognitiveRuntime, _register_host_session,
                                   _resolve_host_session)
from runtime_host.interceptor import install_host_interceptor


__all__ = [
    "ActionDescriptor", "AgentSessionHandle", "BehavioralAdvisor",
    "CompletionClaim", "CognitiveRuntime", "GateDecision", "HostExecutionContext",
    "HostSessionHandle", "RuntimeBundle", "RuntimeSession", "RuntimeTaskHandle",
    "SharedWorkflowMembershipAuthority", "TaskRequest", "effective_action_registry",
    "install_host_interceptor", "resolve", "start",
]
