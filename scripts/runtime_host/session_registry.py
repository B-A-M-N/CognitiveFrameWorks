"""Bounded ownership and lifecycle registry for CFW host session handles."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable


@dataclass
class _RegisteredSession:
    token: str
    session: Any
    owner_identity: str
    expires_at: float


class HostSessionRegistry:
    """Bound handle retention with owner pinning and explicit close semantics."""

    def __init__(self, *, max_sessions: int = 512,
                 ttl_seconds: float = 6 * 60 * 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if type(max_sessions) is not int or not 1 <= max_sessions <= 16_384:
            raise ValueError("host session registry bound must be between 1 and 16384")
        if not isinstance(ttl_seconds, (int, float)) or not 60 <= ttl_seconds <= 7 * 24 * 3600:
            raise ValueError("host session registry TTL must be between one minute and seven days")
        self.max_sessions = max_sessions
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._entries: OrderedDict[str, _RegisteredSession] = OrderedDict()
        self._lock = threading.RLock()

    def register(self, session: Any) -> str:
        owner_identity = self._owner_identity(session)
        token = f"host-session-{uuid.uuid4().hex}"
        with self._lock:
            self._expire_locked()
            while len(self._entries) >= self.max_sessions:
                token, _entry = next(iter(self._entries.items()))
                self._drop_locked(token)
            self._entries[token] = _RegisteredSession(
                token=token, session=session, owner_identity=owner_identity,
                expires_at=self._clock() + self.ttl_seconds)
        return token

    def resolve(self, token: str) -> Any:
        with self._lock:
            self._expire_locked()
            entry = self._entries.get(token)
            if entry is None:
                raise RuntimeError("unknown or expired host session handle")
            if self._owner_identity(entry.session) != entry.owner_identity:
                self._drop_locked(token)
                raise RuntimeError("host session handle ownership identity changed")
            entry.expires_at = self._clock() + self.ttl_seconds
            self._entries.move_to_end(token)
            return entry.session

    def close(self, token: str) -> None:
        with self._lock:
            self._drop_locked(token)

    def close_all(self) -> None:
        with self._lock:
            entries = tuple(self._entries.values())
            self._entries.clear()
            closed: set[int] = set()
            for entry in entries:
                key = id(entry.session)
                if key not in closed:
                    self._close_resources(entry.session)
                    closed.add(key)

    def __len__(self) -> int:
        with self._lock:
            self._expire_locked()
            return len(self._entries)

    def _expire_locked(self) -> None:
        now = self._clock()
        expired = [token for token, entry in self._entries.items()
                   if entry.expires_at <= now]
        for token in expired:
            self._drop_locked(token)

    def _drop_locked(self, token: str) -> None:
        entry = self._entries.pop(token, None)
        if entry is not None and not any(
                other.session is entry.session for other in self._entries.values()):
            self._close_resources(entry.session)

    @staticmethod
    def _owner_identity(session: Any) -> str:
        execution = getattr(session, "execution_identity", None)
        if execution is not None:
            storage_key = getattr(execution, "storage_key", None)
            if callable(storage_key):
                value: Any = storage_key()
            else:
                to_dict = getattr(execution, "to_dict", None)
                value = to_dict() if callable(to_dict) else execution
        else:
            bundle = getattr(session, "bundle", None)
            value = {
                "session_id": getattr(session, "session_id", None),
                "agent_instance_id": getattr(session, "agent_instance_id", None),
                "attempt_id": getattr(session, "attempt_id", None),
                "task_id": getattr(bundle, "task_id", None),
            }
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError("host session owner identity is not serializable") from exc
        if not encoded or encoded == "null":
            raise ValueError("host session requires an owner identity")
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _close_resources(session: Any) -> None:
        exporter = getattr(session, "_telemetry_exporter", None)
        close = getattr(exporter, "close", None)
        if callable(close):
            close()


HOST_SESSION_REGISTRY = HostSessionRegistry()


__all__ = ["HOST_SESSION_REGISTRY", "HostSessionRegistry"]
