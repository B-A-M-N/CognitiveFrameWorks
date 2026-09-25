"""Read-only Cognitive Cockpit service. It never exposes authority handles."""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from runtime_contract.operator_events import OperatorEvent
from runtime_contract.operator_readmodel import OperatorReadModel, ResyncRequired


class OperatorService:
    def __init__(self, *, state_path: Path, csw_projection: Any = None,
                 dp_projection: Any = None) -> None:
        self.read_model = OperatorReadModel(state_path)
        self.csw_projection = csw_projection
        self.dp_projection = dp_projection
        self._tasks: dict[str, Any] = {}
        self._lock = threading.RLock()

    def register_task(self, task_id: str, host_handle: Any) -> None:
        with self._lock:
            self._tasks[str(task_id)] = host_handle

    def unregister_task(self, task_id: str) -> None:
        with self._lock:
            handle = self._tasks.pop(str(task_id), None)
        if handle is not None:
            close = getattr(handle, "close", None)
            if callable(close): close()

    def publish_runtime_event(self, event: OperatorEvent) -> bool:
        return self.read_model.publish(event)

    def wait_for_sequence(self, sequence: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if int(self.read_model.snapshot().get("high_watermark", 0)) >= sequence:
                return True
            time.sleep(0.01)
        return False

    def snapshot(self) -> dict[str, Any]:
        snapshot = self.read_model.snapshot()
        tasks = []
        with self._lock:
            handles = dict(self._tasks)
        for task_id, handle in handles.items():
            try:
                trace = handle.operator_trace()
                tasks.append({
                    "task_id": task_id,
                    "agent": trace.get("task", {}),
                    "policy": trace.get("policy", {}),
                    "workflow": trace.get("statework", []),
                    "status": "running" if trace.get("task") else "unknown",
                    "provenance": "host_verified",
                })
            except Exception as exc:
                tasks.append({"task_id": task_id, "status": "disconnected",
                              "reason_code": type(exc).__name__, "provenance": "host_verified"})
        snapshot["tasks"] = tasks
        snapshot["workflows"] = self.csw_projection.snapshot() if self.csw_projection is not None else []
        dp_snapshot = self.dp_projection.snapshot() if self.dp_projection is not None else {"profiles": []}
        if isinstance(dp_snapshot, dict):
            snapshot["profiles"] = list(dp_snapshot.get("profiles") or [])
            snapshot.setdefault("health", {})["dp"] = dict(dp_snapshot.get("health") or {})
        else:
            snapshot["profiles"] = list(dp_snapshot)
        snapshot["health"] = {
            "operator_event_high_watermark": snapshot.get("high_watermark", 0),
            "subscriptions": "bounded",
            "authority": "read_only",
        }
        return snapshot

    def stream(self, after: int):
        return self.read_model.stream(after)

    def close(self) -> None:
        with self._lock:
            self._tasks.clear()
        self.read_model.close()
