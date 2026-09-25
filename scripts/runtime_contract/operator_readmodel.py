"""Bounded durable read model for the optional Cognitive Cockpit."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Iterator

from .operator_events import OperatorEvent


class ResyncRequired(RuntimeError):
    pass



class OperatorSubscription:
    def __init__(self, read_model: "OperatorReadModel", after: int) -> None:
        self._model = read_model
        self.after = int(after)
        self._queue: queue.Queue = queue.Queue(maxsize=read_model.retention)
        with read_model._lock:
            read_model._subscribers.add(self._queue)
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not hasattr(self, "_iterator"):
            self._iterator = self._iterate()
        return next(self._iterator)

    def _iterate(self):
        yield from self._model._backlog(self.after, self._queue)
        while not self._closed:
            value = self._queue.get()
            if value is None:
                return
            self.after = int(value.get("sequence", self.after))
            yield value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._model._lock:
            self._model._subscribers.discard(self._queue)

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

class OperatorReadModel:
    """Single async projection worker; observers receive only sanitized events."""
    def __init__(self, path: Path, *, retention: int = 10_000,
                 queue_size: int = 2_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.retention = int(retention)
        self._queue: queue.Queue[OperatorEvent | None] = queue.Queue(maxsize=queue_size)
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.RLock()
        self._worker = threading.Thread(target=self._run, daemon=True, name="cognitive-cockpit-readmodel")
        with sqlite3.connect(str(self.path), timeout=1) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS operator_events (
                sequence INTEGER PRIMARY KEY, event_json TEXT NOT NULL)""")
        self._worker.start()

    def allocate_sequence(self) -> int:
        with self._lock, sqlite3.connect(str(self.path), timeout=1) as connection:
            row = connection.execute("SELECT MAX(sequence) FROM operator_events").fetchone()
            return int(row[0] or 0) + 1

    def publish(self, event: OperatorEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            return False

    def snapshot(self) -> dict[str, Any]:
        with sqlite3.connect(str(self.path), timeout=1) as connection:
            row = connection.execute("SELECT MAX(sequence) FROM operator_events").fetchone()
            high_watermark = int(row[0] or 0)
            latest = connection.execute(
                "SELECT event_json FROM operator_events ORDER BY sequence DESC LIMIT 200"
            ).fetchall()
        return {"schema_version": "1.0.0", "high_watermark": high_watermark,
                "events": [json.loads(item[0]) for item in reversed(latest)],
                "statuses": {}, "workflows": {}, "profiles": {}, "health": {}}

    def stream(self, after: int) -> "OperatorSubscription":
        return OperatorSubscription(self, after)

    def _backlog(self, after: int, subscriber: queue.Queue) -> Iterator[dict[str, Any]]:
        with sqlite3.connect(str(self.path), timeout=1) as connection:
            rows = connection.execute(
                "SELECT sequence,event_json FROM operator_events WHERE sequence > ? ORDER BY sequence",
                (int(after),)).fetchall()
            earliest = connection.execute("SELECT MIN(sequence) FROM operator_events").fetchone()[0]
        if earliest is not None and int(after) + 1 < int(earliest):
            raise ResyncRequired("operator event retention expired; request a new snapshot")
        for sequence, raw in rows:
            value = json.loads(raw)
            try:
                subscriber.put_nowait(value)
            except queue.Full:
                raise ResyncRequired("subscriber fell behind retained operator events")
            yield value

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(1)
        with self._lock:
            for subscriber in tuple(self._subscribers):
                try: subscriber.put_nowait(None)
                except queue.Full: pass
            self._subscribers.clear()

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                with self._lock, sqlite3.connect(str(self.path), timeout=1) as connection:
                    connection.execute("INSERT OR IGNORE INTO operator_events VALUES (?,?)",
                                       (event.sequence, event.to_json()))
                    connection.execute("""DELETE FROM operator_events WHERE sequence <= (
                        SELECT MAX(sequence) - ? FROM operator_events)""", (self.retention,))
                    value = event.to_dict()
                    for subscriber in tuple(self._subscribers):
                        try: subscriber.put_nowait(value)
                        except queue.Full: pass
            except Exception:
                continue
