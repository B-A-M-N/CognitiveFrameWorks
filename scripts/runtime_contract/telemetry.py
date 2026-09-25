"""Canonical DP behavior-event schema contract for host telemetry."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
import time
import fcntl
import threading
from typing import Callable, Iterable, Mapping
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, Dict, Tuple

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


class DurableTelemetryExporter:
    """Bounded-offset DP delivery with explicit production acknowledgements.

    Local telemetry remains the source record. The exporter reads only bytes
    after its durable cursor, never advances past an incomplete trailing line,
    and never assumes delivery without an explicit acknowledgement identity.
    """
    def __init__(self, source: Path, sink: Callable[[list[dict[str, Any]]], Any],
                 *, state_dir: Path, batch_size: int = 64,
                 max_delivered_ids: int = 2048,
                 origin_budget: Mapping[str, int] | None = None):
        if not 1 <= batch_size <= 1000:
            raise ValueError("telemetry exporter batch_size must be 1..1000")
        if not 1 <= max_delivered_ids <= 100_000:
            raise ValueError("telemetry exporter max_delivered_ids must be 1..100000")
        if origin_budget is not None and any(int(value) < 1 for value in origin_budget.values()):
            raise ValueError("telemetry origin budgets must be positive")
        self.source = Path(source)
        self.sink = sink
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.cursor_path = self.state_dir / "telemetry.cursor.json"
        self.ids_path = self.state_dir / "telemetry.ids.json"
        self.batch_size = batch_size
        self.max_delivered_ids = max_delivered_ids
        self.origin_budget = {str(key): int(value) for key, value in (origin_budget or {}).items()}
        self.lock_path = self.state_dir / "telemetry.exporter.lock"
        self._inflight = False

    def _cursor(self) -> dict[str, Any]:
        try:
            data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
            return {
                "byte_offset": max(0, int(data.get("byte_offset", 0) or 0)),
                "last_event_id": str(data.get("last_event_id") or ""),
            }
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            return {"byte_offset": 0, "last_event_id": ""}

    def _save_cursor(self, *, byte_offset: int, last_event_id: str) -> None:
        fd, temp = tempfile.mkstemp(dir=self.state_dir, prefix=".telemetry-cursor.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"byte_offset": max(0, int(byte_offset)),
                           "last_event_id": str(last_event_id or "")},
                          stream, sort_keys=True, separators=(",", ":"))
                stream.flush(); os.fsync(stream.fileno())
            os.chmod(temp, 0o600); os.replace(temp, self.cursor_path)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def _delivered_ids(self) -> list[str]:
        try:
            data = json.loads(self.ids_path.read_text(encoding="utf-8"))
            return [str(value) for value in data][-self.max_delivered_ids:] if isinstance(data, list) else []
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            return []

    def _save_ids(self, ids: Iterable[str]) -> None:
        bounded = list(dict.fromkeys(str(value) for value in ids))[-self.max_delivered_ids:]
        fd, temp = tempfile.mkstemp(dir=self.state_dir, prefix=".telemetry-ids.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(bounded, stream, separators=(",", ":"))
                stream.flush(); os.fsync(stream.fileno())
            os.chmod(temp, 0o600); os.replace(temp, self.ids_path)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def flush(self) -> int:
        if not self.source.exists():
            return 0
        size = self.source.stat().st_size
        cursor = self._cursor()
        start = cursor["byte_offset"]
        if start > size:
            # Source rotation/truncation starts a new generation; do not seek
            # into the middle of an unrelated replacement file.
            start = 0
        delivered = self._delivered_ids()
        delivered_set = set(delivered)
        with self.source.open("rb") as stream:
            stream.seek(start)
            entries: list[tuple[dict[str, Any], int]] = []
            while len(entries) < self.batch_size:
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                line_end = stream.tell()
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # A malformed complete line cannot become valid by
                    # appending more data; skip exactly that line.
                    continue
                if not isinstance(value, dict):
                    continue
                event_id = str(value.get("event_id") or "")
                if not event_id or event_id in delivered_set:
                    entries.append((value, line_end))
                    continue
                entries.append((value, line_end))
        if not entries:
            self._save_cursor(byte_offset=start, last_event_id=cursor["last_event_id"])
            return 0
        pending = [value for value, _ in entries
                   if str(value.get("event_id") or "") not in delivered_set]
        if not pending:
            final_entry = entries[-1]
            self._save_cursor(byte_offset=final_entry[1],
                              last_event_id=str(final_entry[0].get("event_id") or cursor["last_event_id"]))
            return 0
        origins = {
            (str(item.get("namespace_id") or "default"),
             str(item.get("application_instance_id") or "unknown-application"),
             str(item.get("agent_instance_id") or "unknown-agent"))
            for item in pending
        }
        if any(sum(1 for item in pending if (
                str(item.get("namespace_id") or "default"),
                str(item.get("application_instance_id") or "unknown-application"),
                str(item.get("agent_instance_id") or "unknown-agent")) == origin)
               > self.origin_budget.get("|".join(origin), 1 << 30)
               for origin in origins):
            return 0
        deadline = time.monotonic() + min(15.0, max(0.1, self.batch_size / 10))
        result: list[Any] = []
        completed = threading.Event()
        def deliver() -> None:
            try:
                result.append(self.sink(pending))
            finally:
                completed.set()
                self._inflight = False
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            if self._inflight:
                return 0
            self._inflight = True
            worker = threading.Thread(target=deliver, daemon=True,
                                      name="cognitiveframeworks-telemetry-sink")
            worker.start()
            finished = completed.wait(max(0.01, deadline - time.monotonic()))
        if not finished:
            return 0
        self._inflight = False
        acknowledged = result[0] if result else None
        if acknowledged is None:
            raise ValueError("telemetry sink must explicitly acknowledge delivered event identities")
        if isinstance(acknowledged, (set, list, tuple)):
            acknowledged_ids = {str(value) for value in acknowledged}
        elif isinstance(acknowledged, Mapping):
            acknowledged_ids = {str(key) for key, value in acknowledged.items() if value}
        else:
            raise ValueError("telemetry sink must return acknowledged event identities")
        if not acknowledged_ids:
            return 0
        consumed = 0
        final_offset = start
        last_id = cursor["last_event_id"]
        newly_acked: list[str] = []
        for event, line_end in entries:
            event_id = str(event.get("event_id") or "")
            if event_id in delivered_set:
                consumed += 1
                final_offset = line_end
                last_id = event_id
                continue
            if event_id not in acknowledged_ids:
                break
            consumed += 1
            final_offset = line_end
            last_id = event_id
            newly_acked.append(event_id)
            delivered_set.add(event_id)
        if consumed:
            delivered.extend(newly_acked)
            self._save_ids(delivered)
            self._save_cursor(byte_offset=final_offset, last_event_id=last_id)
        return consumed
