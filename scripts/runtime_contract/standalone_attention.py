"""Deterministic bounded attention for standalone CFW task execution."""
from __future__ import annotations

from typing import Mapping, Sequence

from .system_one import CriterionState


class StandaloneAttentionPolicy:
    """Rotate among unfinished criteria and yield after repeated local failure."""

    WINDOW_SIZE = 3
    RETRY_YIELD_THRESHOLD = 2

    @classmethod
    def runnable(cls, criteria: Sequence[CriterionState]) -> tuple[CriterionState, ...]:
        return tuple(item for item in criteria
                     if item.status not in {"satisfied", "blocked"})

    @classmethod
    def select(cls, criteria: Sequence[CriterionState], *,
               retry_counts: Mapping[str, int],
               last_action_sequence: Mapping[str, int]) -> tuple[str, ...]:
        runnable = cls.runnable(criteria)
        ordered = sorted(runnable, key=lambda item: (
            retry_counts.get(item.criterion_id, 0) >= cls.RETRY_YIELD_THRESHOLD,
            last_action_sequence.get(item.criterion_id, 0),
            retry_counts.get(item.criterion_id, 0),
            item.criterion_id,
        ))
        return tuple(item.criterion_id for item in ordered[:cls.WINDOW_SIZE])


__all__ = ["StandaloneAttentionPolicy"]
