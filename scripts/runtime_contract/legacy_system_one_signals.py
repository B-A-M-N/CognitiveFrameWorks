"""Legacy System-1 signal records retained for historical artifact readers.

The live CFW runtime does not import or persist these records. Semantic
assessment and its task-scoped state belong to DigitalPsychology.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(frozen=True)
class SemanticSignals:
    """Legacy semantic observations; they carry no execution mode or authority."""

    action_relation: str | None = None
    progress_relation: str | None = None
    equivalent_retry: bool | None = None
    possible_goal_drift: bool | None = None
    route_preference: str | None = None
    source_decisions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"action_relation": self.action_relation,
                "progress_relation": self.progress_relation,
                "equivalent_retry": self.equivalent_retry,
                "possible_goal_drift": self.possible_goal_drift,
                "route_preference": self.route_preference,
                "source_decisions": list(self.source_decisions)}


@dataclass(frozen=True)
class SemanticSignalRecord:
    question_family: str
    scenario: str
    criterion_id: str | None
    cause_id: str | None
    candidate_digest: str
    metacognitive_revision: int
    value: Any
    confidence: float | None
    source_decision_id: str

    def to_dict(self) -> dict[str, object]:
        return {"question_family": self.question_family, "scenario": self.scenario,
                "criterion_id": self.criterion_id, "cause_id": self.cause_id,
                "candidate_digest": self.candidate_digest,
                "metacognitive_revision": self.metacognitive_revision,
                "value": self.value, "confidence": self.confidence,
                "source_decision_id": self.source_decision_id}


class SemanticSignalStore:
    """Legacy bounded semantic records, isolated from live CFW working state."""

    _FIELDS: ClassVar[dict[str, str]] = {
        "action_relation": "action_relation",
        "progress_relation": "progress_relation",
        "retry_relation": "equivalent_retry",
        "goal_drift": "possible_goal_drift",
        "route_preference": "route_preference",
    }

    def __init__(self, *, limit: int = 128) -> None:
        if not 1 <= limit <= 512:
            raise ValueError("semantic signal store limit must be between 1 and 512")
        self.limit = limit
        self._records: dict[tuple[Any, ...], SemanticSignalRecord] = {}

    @property
    def records(self) -> tuple[SemanticSignalRecord, ...]:
        return tuple(self._records.values())

    def record(self, *, question_family: str, value: Any, scenario: str,
               candidate_digest: str, metacognitive_revision: int,
               source_decision_id: str, criterion_id: str | None = None,
               cause_id: str | None = None,
               confidence: float | None = None) -> None:
        if question_family not in self._FIELDS:
            raise ValueError("semantic signal family is not registered")
        if not scenario or not source_decision_id or not candidate_digest:
            raise ValueError("semantic signal requires scenario, candidate, and decision identities")
        if len(candidate_digest) != 64 or metacognitive_revision < 0:
            raise ValueError("semantic signal candidate digest or revision is invalid")
        if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("semantic signal confidence must be between zero and one")
        record = SemanticSignalRecord(
            question_family=question_family, scenario=scenario[:128],
            criterion_id=criterion_id[:128] if criterion_id else None,
            cause_id=cause_id[:128] if cause_id else None,
            candidate_digest=candidate_digest,
            metacognitive_revision=int(metacognitive_revision), value=value,
            confidence=float(confidence) if confidence is not None else None,
            source_decision_id=source_decision_id[:256])
        key = (record.question_family, record.scenario, record.criterion_id,
               record.cause_id, record.candidate_digest, record.metacognitive_revision)
        self._records[key] = record
        while len(self._records) > self.limit:
            self._records.pop(next(iter(self._records)))

    def invalidate(self, *, question_families: set[str] | None = None,
                   scenario: str | None = None, criterion_id: str | None = None,
                   cause_id: str | None = None,
                   candidate_digest: str | None = None) -> int:
        removed = []
        for key, item in self._records.items():
            if question_families is not None and item.question_family not in question_families:
                continue
            if scenario is not None and item.scenario != scenario:
                continue
            if criterion_id is not None and item.criterion_id != criterion_id:
                continue
            if cause_id is not None and item.cause_id != cause_id:
                continue
            if candidate_digest is not None and item.candidate_digest != candidate_digest:
                continue
            removed.append(key)
        for key in removed:
            del self._records[key]
        return len(removed)

    def snapshot(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in self._records.values()]

    def restore(self, records: Sequence[Mapping[str, Any]]) -> None:
        if len(records) > self.limit:
            raise ValueError("semantic signal snapshot exceeds configured bound")
        self._records.clear()
        for row in records:
            self.record(
                question_family=str(row.get("question_family") or ""),
                value=row.get("value"), scenario=str(row.get("scenario") or ""),
                candidate_digest=str(row.get("candidate_digest") or ""),
                metacognitive_revision=int(row.get("metacognitive_revision", -1)),
                source_decision_id=str(row.get("source_decision_id") or ""),
                criterion_id=(str(row["criterion_id"])
                              if row.get("criterion_id") else None),
                cause_id=(str(row["cause_id"]) if row.get("cause_id") else None),
                confidence=(float(row["confidence"])
                            if row.get("confidence") is not None else None))

    def current(self, *, metacognitive_revision: int,
                question_families: set[str] | None = None) -> SemanticSignals:
        eligible = [item for item in self._records.values()
                    if item.metacognitive_revision == int(metacognitive_revision)
                    and (question_families is None
                         or item.question_family in question_families)]
        if not eligible:
            return SemanticSignals()
        # Do not fuse records from different candidate contexts.
        latest = eligible[-1]
        context = [item for item in eligible
                   if (item.scenario, item.candidate_digest, item.cause_id)
                   == (latest.scenario, latest.candidate_digest, latest.cause_id)]
        values: dict[str, Any] = {}
        sources: list[str] = []
        for item in context:
            values[self._FIELDS[item.question_family]] = item.value
            sources.append(item.source_decision_id)
        return SemanticSignals(**values,
                               source_decisions=tuple(dict.fromkeys(sources))[:8])


__all__ = ["SemanticSignalRecord", "SemanticSignalStore", "SemanticSignals"]
