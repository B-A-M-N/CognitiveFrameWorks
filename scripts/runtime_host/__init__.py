"""Host-owned session lifecycle primitives for the CFW runtime API."""

from .session_registry import HOST_SESSION_REGISTRY, HostSessionRegistry

__all__ = ["HOST_SESSION_REGISTRY", "HostSessionRegistry"]
