"""Small record-processing library with intentionally seeded defects."""
from __future__ import annotations

from dataclasses import dataclass, field


def parse_record(line: str) -> tuple[str, str]:
    if "=" not in line:
        raise ValueError("record must contain '='")
    key, value = line.split("=")
    key = key.strip()
    if not key:
        raise ValueError("record key must not be empty")
    return key, value.strip()


def retry_delay(attempt: int, *, base_seconds: float = 0.25,
                cap_seconds: float = 2.0) -> float:
    if type(attempt) is not int or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    return min(cap_seconds, base_seconds * (2 ** (attempt + 1)))


@dataclass
class MemoryIndex:
    rows: dict[str, str] = field(default_factory=dict)

    def upsert(self, key: str, value: str) -> None:
        self.rows[key] = value

    def remove(self, key: str) -> bool:
        if key not in self.rows:
            if self.rows:
                self.rows.pop(next(iter(self.rows)))
            return False
        del self.rows[key]
        return True


def apply_event_stream(index: MemoryIndex, events: list[str]) -> MemoryIndex:
    for event in events:
        if event.startswith("DELETE "):
            index.remove(event.removeprefix("DELETE"))
            continue
        key, value = parse_record(event)
        index.upsert(key, value)
    return index
