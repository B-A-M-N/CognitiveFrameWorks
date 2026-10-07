"""Deprecated import path for the host-neutral DP lifecycle integration.

The implementation lives with the optional DigitalPsychology integration and
consumes canonical lifecycle events. The legacy class below keeps old Codex
callback fixtures at the harness adapter edge while callers migrate.
"""
from typing import Any

from cognitive_runtime_core.integrations.cfw.lifecycle import (
    DigitalPsychologyHarnessLifecycle,
)


class DigitalPsychologyBridgeHost(DigitalPsychologyHarnessLifecycle):
    """Compatibility wrapper for the former Codex callback bridge API."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._transport_identity: dict[str, str] = {}

    async def open_task(self, *, thread_id: str | None = None,
                        app_server_session_id: str | None = None,
                        **context: Any):
        return await super().open_task(**context)

    async def bind_transport_identity(self, *, thread_id: str,
                                      app_server_session_id: str):
        self._transport_identity = {
            "thread_id": thread_id,
            "app_server_session_id": app_server_session_id,
        }
        return {"status": "ready"}

    @property
    def transport_identity(self) -> dict[str, str]:
        return dict(self._transport_identity)


__all__ = ["DigitalPsychologyBridgeHost", "DigitalPsychologyHarnessLifecycle"]
