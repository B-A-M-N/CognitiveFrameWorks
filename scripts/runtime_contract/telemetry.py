"""Canonical DP behavior-event schema contract for host telemetry."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import queue
import tempfile
import threading
import time
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Tuple

EventKey = Tuple[str, str, str]

def behavior_schema_contract(path: Path) -> Tuple[Dict[str, Any], str, str]:
    packaged = resource_files("runtime_contract").joinpath(f"contracts/{path.name}")
    if packaged.is_file():
        raw = packaged.read_bytes()
        schema = json.loads(raw.decode("utf-8"))
        version = str(schema.get("properties", {}).get("schema_version", {}).get("const", ""))
        return schema, hashlib.sha256(raw).hexdigest(), version
    if not path.exists():
        candidates = [Path(__file__).resolve().parents[2] / "contracts" / path.name,
                      Path(__file__).resolve().parents[1] / "contracts" / path.name]
        for local in candidates:
            if local.exists():
                path = local
                break
    if not path.exists():
        raise ValueError(f"behavior-event schema is unavailable: {path}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    version = str(schema.get("properties", {}).get("schema_version", {}).get("const", ""))
    return schema, hashlib.sha256(path.read_bytes()).hexdigest(), version


def event_key(event: Mapping[str, Any]) -> EventKey:
    return (str(event.get("namespace_id") or "default"),
            str(event.get("application_instance_id") or "unknown-application"),
            str(event.get("event_id") or ""))


def _encode_key(key: EventKey) -> str:
    return json.dumps(list(key), separators=(",", ":"))


def _decode_key(value: Any) -> EventKey:
    if isinstance(value, str):
        parsed = json.loads(value)
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        parsed = list(value)
    else:
        raise ValueError("telemetry acknowledgement is not an EventKey")
    return tuple(str(item) for item in parsed)  # type: ignore[return-value]


class DurableTelemetryExporter:
    """Single-owner bounded exporter with durable cursor, keys, and lease.

    ``enqueue`` never calls the sink. One background worker owns delivery and
    cursor updates, preventing overlapping timed-out deliveries.
    """
    def __init__(self, source: Path, sink: Callable[[list[dict[str, Any]]], Any],
                 *, state_dir: Path, batch_size: int = 64,
                 max_delivered_ids: int = 2048,
                 origin_budget: Mapping[str, int] | None = None,
                 queue_size: int = 256, worker_poll_seconds: float = 0.2):
        if not 1 <= batch_size <= 1000:
            raise ValueError("telemetry exporter batch_size must be 1..1000")
        if not 1 <= max_delivered_ids <= 100_000 or queue_size < batch_size:
            raise ValueError("telemetry exporter storage/queue limits are invalid")
        self.source = Path(source)
        self.sink = sink
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.cursor_path = self.state_dir / "telemetry.cursor.json"
        self.ids_path = self.state_dir / "telemetry.ids.json"
        self.lease_path = self.state_dir / "telemetry.lease.json"
        self.lock_path = self.state_dir / "telemetry.exporter.lock"
        self.stop_path = self.state_dir / "telemetry.stopped"
        self._closed = False
        self.batch_size = batch_size
        self.max_delivered_ids = max_delivered_ids
        self.origin_budget = {str(key): int(value) for key, value in (origin_budget or {}).items()}
        self._queue: queue.Queue[None] = queue.Queue(maxsize=queue_size)
        self._worker = threading.Thread(target=self._delivery_loop, daemon=True,
                                        name="cognitiveframeworks-telemetry-owner")
        self._worker.start()

    def _cursor(self) -> dict[str, Any]:
        try:
            data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
            return {"byte_offset": max(0, int(data.get("byte_offset", 0) or 0)),
                    "last_event_key": str(data.get("last_event_key") or "")}
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            return {"byte_offset": 0, "last_event_key": ""}

    def _atomic_json(self, path: Path, value: Any) -> None:
        fd, temp = tempfile.mkstemp(dir=self.state_dir, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, separators=(",", ":"))
                stream.flush(); os.fsync(stream.fileno())
            os.chmod(temp, 0o600); os.replace(temp, path)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def _save_cursor(self, *, byte_offset: int, last_key: EventKey | None) -> None:
        self._atomic_json(self.cursor_path, {
            "byte_offset": max(0, byte_offset),
            "last_event_key": _encode_key(last_key) if last_key else "",
        })

    def _delivered_keys(self) -> list[EventKey]:
        try:
            data = json.loads(self.ids_path.read_text(encoding="utf-8"))
            return [_decode_key(value) for value in data][-self.max_delivered_ids:] if isinstance(data, list) else []
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            return []

    def _save_keys(self, keys: Iterable[EventKey]) -> None:
        bounded = list(dict.fromkeys(keys))[-self.max_delivered_ids:]
        self._atomic_json(self.ids_path, [_encode_key(key) for key in bounded])

    def enqueue(self) -> bool:
        try:
            self._queue.put_nowait(None)
            return True
        except queue.Full:
            return False

    def close(self, timeout: float = 0.2) -> None:
        # Never block task shutdown on a slow receiver. A persistent tombstone
        # prevents a later close/reopen from accidentally restarting delivery.
        self._closed = True
        self._worker.join(timeout)

    def _read_entries(self, start: int) -> list[tuple[dict[str, Any], int]]:
        if not self.source.exists():
            return []
        size = self.source.stat().st_size
        if start > size:
            start = 0
        entries: list[tuple[dict[str, Any], int]] = []
        with self.source.open("rb") as stream:
            stream.seek(start)
            while len(entries) < self.batch_size * 4:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and event_key(value)[2]:
                    entries.append((value, stream.tell()))
        return entries

    def _fair_batch(self, entries, delivered: set[EventKey]) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        order: list[tuple[str, str, str]] = []
        for value, _ in entries:
            key = event_key(value)
            if key in delivered:
                continue
            origin = (key[0], key[1], str(value.get("agent_instance_id") or "unknown-agent"))
            if origin not in buckets:
                buckets[origin] = []; order.append(origin)
            buckets[origin].append(value)
        result: list[dict[str, Any]] = []
        origins = list(order)
        while origins and len(result) < self.batch_size:
            following = []
            for origin in origins:
                values = buckets[origin]
                budget = self.origin_budget.get("|".join(origin), self.batch_size)
                if sum(1 for item in result if event_key(item) in {
                        event_key(v) for v in buckets[origin]}) >= budget:
                    following.append(origin); continue
                if values:
                    result.append(values.pop(0))
                    if len(result) >= self.batch_size:
                        break
                if values:
                    following.append(origin)
            if not following and len(result) >= self.batch_size:
                break
            origins = following
        return result

    def _lease(self) -> bool:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                if self.lease_path.exists():
                    lease = json.loads(self.lease_path.read_text(encoding="utf-8"))
                    if float(lease.get("expires_at", 0)) > time.time():
                        return False
                self._atomic_json(self.lease_path, {
                    "owner": threading.get_ident(), "expires_at": time.time() + min(5.0, max(0.1, self.batch_size / 10)),
                })
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _release_lease(self) -> None:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.lease_path.exists(): self.lease_path.unlink()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _delivery_loop(self) -> None:
        while True:
            try:
                self._queue.get(timeout=0.2)
                self._drain_once()
            except queue.Empty:
                continue
            except Exception:
                continue

    def _drain_once(self) -> int:
        if self._closed:
            return 0
        if not self._lease():
            return 0
        try:
            cursor = self._cursor(); start = cursor["byte_offset"]
            entries = self._read_entries(start)
            if not entries:
                self._save_cursor(byte_offset=start, last_key=None); return 0
            delivered = self._delivered_keys(); delivered_set = set(delivered)
            pending = self._fair_batch(entries, delivered_set)
            if not pending:
                return 0
            deadline = time.monotonic() + min(15.0, max(0.1, self.batch_size / 10))
            try:
                acknowledged = self.sink(pending)
            except Exception:
                return 0
            if time.monotonic() > deadline:
                # A timed-out/ambiguous response is retried. Receiver idempotency
                # is required; it is not safe to advance the cursor.
                return 0
            if acknowledged is None:
                raise ValueError("telemetry sink must explicitly acknowledge EventKey identities")
            if isinstance(acknowledged, (set, list, tuple)):
                ack_keys = {_decode_key(value) for value in acknowledged}
            elif isinstance(acknowledged, Mapping):
                ack_keys = {_decode_key(key) for key, value in acknowledged.items() if value}
            else:
                raise ValueError("telemetry sink must return EventKey identities")
            allowed = {event_key(value) for value in pending}
            if not ack_keys.issubset(allowed):
                raise ValueError("telemetry sink acknowledged unknown or cross-origin EventKey")
            consumed = 0; final_offset = start; last_key = None
            newly: list[EventKey] = []
            for value, line_end in entries:
                key = event_key(value)
                if key in delivered_set:
                    consumed += 1; final_offset = line_end; last_key = key
                elif key in ack_keys:
                    consumed += 1; final_offset = line_end; last_key = key; newly.append(key)
                else:
                    break
            if consumed:
                delivered.extend(newly); self._save_keys(delivered)
                self._save_cursor(byte_offset=final_offset, last_key=last_key)
            return consumed
        finally:
            self._release_lease()

    def flush(self) -> int:
        """Enqueue delivery and return immediately.

        Tests may call ``_drain_once`` for deterministic synchronous evidence.
        """
        self.enqueue()
        return 0
