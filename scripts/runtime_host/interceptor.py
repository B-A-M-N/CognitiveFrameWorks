"""Compatibility registration hook for host interceptor registries."""
from __future__ import annotations

from typing import Any

from runtime_host.host_api import CognitiveRuntime


def install_host_interceptor(registry: Any, interceptor: CognitiveRuntime) -> None:
    """Register the runtime API with a host-provided interceptor registry."""
    register = getattr(registry, "register_host_interceptor", None)
    if not callable(register):
        raise TypeError("host registry must provide register_host_interceptor")
    register(interceptor)


__all__ = ["install_host_interceptor"]
