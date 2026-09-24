"""Canonical DP behavior-event schema contract for host telemetry."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
from typing import Callable, Iterable
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
    """Optional non-blocking DP delivery with a durable cursor.

    Local telemetry remains the source record. The exporter is advisory: a
    failed remote call returns ``False`` and never changes task authority.
    It uses a bounded batch, stable event IDs, and a cursor file for replay.
    """
    def __init__(self, source: Path, sink: Callable[[list[dict[str, Any]]], Any],
                 *, state_dir: Path, batch_size: int = 64):
        if not 1 <= batch_size <= 1000:
            raise ValueError("telemetry exporter batch_size must be 1..1000")
        self.source = Path(source)
        self.sink = sink
        self.state_dir = Path(state_dir); self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.cursor_path = self.state_dir / "telemetry.cursor"
        self.ids_path = self.state_dir / "telemetry.ids.json"
        self.batch_size = batch_size

    def _cursor(self) -> int:
        try:
            return max(0, int(self.cursor_path.read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            return 0

    def _save_cursor(self, value: int) -> None:
        fd, temp = tempfile.mkstemp(dir=self.state_dir, prefix=".telemetry-cursor.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(str(value) + "\n"); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temp, 0o600); os.replace(temp, self.cursor_path)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def _delivered_ids(self) -> set[str]:
        try:
            data = json.loads(self.ids_path.read_text(encoding="utf-8"))
            return {str(value) for value in data} if isinstance(data, list) else set()
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return set()

    def _save_ids(self, ids: set[str]) -> None:
        fd, temp = tempfile.mkstemp(dir=self.state_dir, prefix=".telemetry-ids.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(sorted(ids), stream, separators=(",", ":"))
                stream.flush(); os.fsync(stream.fileno())
            os.chmod(temp, 0o600); os.replace(temp, self.ids_path)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def flush(self) -> int:
        if not self.source.exists():
            return 0
        events = []
        for line in self.source.read_text(encoding="utf-8").splitlines():
            try: events.append(json.loads(line))
            except json.JSONDecodeError: continue
        start = self._cursor(); batch = events[start:start + self.batch_size]
        delivered = self._delivered_ids()
        batch = [event for event in batch if str(event.get("event_id") or "") not in delivered]
        if not batch:
            self._save_cursor(start + min(self.batch_size, len(events) - start))
            return 0
        try:
            acknowledged = self.sink(batch)
        except Exception:
            return 0
        if acknowledged is False:
            return 0
        if acknowledged is None:
            acknowledged_ids = {str(event.get("event_id")) for event in batch if event.get("event_id")}
        elif isinstance(acknowledged, (set, list, tuple)):
            acknowledged_ids = {str(value) for value in acknowledged}
        elif isinstance(acknowledged, Mapping):
            acknowledged_ids = {str(key) for key, value in acknowledged.items() if value}
        else:
            raise ValueError("telemetry sink must return acknowledged event identities")
        if not acknowledged_ids:
            return 0
        delivered.update(acknowledged_ids)
        self._save_ids(delivered)
        consumed = 0
        for event in batch:
            if str(event.get("event_id")) in acknowledged_ids:
                consumed += 1
            else:
                break
        self._save_cursor(start + consumed)
        return consumed
