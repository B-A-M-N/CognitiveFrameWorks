"""Deterministic bounded attention for standalone CFW task execution."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .behavior_facts import ActivityFact, AttemptSemanticDescriptor, CriterionState


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
               last_action_sequence: Mapping[str, int],
               redirect_counts: Mapping[str, int] | None = None) -> tuple[str, ...]:
        redirects = redirect_counts or {}
        runnable = cls.runnable(criteria)
        ordered = sorted(runnable, key=lambda item: (
            max(retry_counts.get(item.criterion_id, 0),
                redirects.get(item.criterion_id, 0)) >= cls.RETRY_YIELD_THRESHOLD,
            last_action_sequence.get(item.criterion_id, 0),
            retry_counts.get(item.criterion_id, 0),
            item.criterion_id,
        ))
        return tuple(item.criterion_id for item in ordered[:cls.WINDOW_SIZE])


class StandaloneAttentionState:
    """Mutable attention and redirect state for the standalone CFW fallback."""

    def __init__(self) -> None:
        self._criterion_action_counts: dict[str, int] = {}
        self._criterion_retry_counts: dict[str, int] = {}
        self._criterion_redirect_counts: dict[str, int] = {}
        self._criterion_last_action_sequence: dict[str, int] = {}
        self._progress_redirect_epoch = 0
        self._last_progress_redirect_action: str | None = None
        self._last_progress_redirect_criterion: str | None = None
        self._progress_redirect_yield_target: str | None = None
        self._progress_redirect_guidance_pending = False
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def criterion_retry_counts(self) -> Mapping[str, int]:
        return dict(self._criterion_retry_counts)

    @property
    def criterion_last_action_sequence(self) -> Mapping[str, int]:
        return dict(self._criterion_last_action_sequence)

    @property
    def progress_redirected(self) -> bool:
        return any(self._criterion_redirect_counts.values())

    @property
    def progress_redirect_guidance_pending(self) -> bool:
        return self._progress_redirect_guidance_pending

    @property
    def progress_redirect_epoch(self) -> int:
        return self._progress_redirect_epoch

    def active_attention_window(self, criteria: Sequence[CriterionState]) -> tuple[str, ...]:
        self.sync_criteria(criteria)
        return StandaloneAttentionPolicy.select(
            criteria, retry_counts=self._criterion_retry_counts,
            last_action_sequence=self._criterion_last_action_sequence,
            redirect_counts=self._criterion_redirect_counts)

    def sync_criteria(self, criteria: Sequence[CriterionState]) -> None:
        ids = {item.criterion_id for item in criteria}
        for values in (self._criterion_action_counts, self._criterion_retry_counts,
                       self._criterion_redirect_counts,
                       self._criterion_last_action_sequence):
            for criterion_id in tuple(values):
                if criterion_id not in ids and criterion_id != "__task__":
                    del values[criterion_id]
            for criterion_id in ids:
                values.setdefault(criterion_id, 0)

    def record_activity(self, fact: ActivityFact) -> None:
        """Update standalone attention from a host-committed local fact."""
        if fact.activity_type in {"action", "retry"}:
            for criterion_id in fact.affected_criterion_ids:
                if criterion_id not in self._criterion_action_counts:
                    continue
                self._criterion_action_counts[criterion_id] = (
                    self._criterion_action_counts.get(criterion_id, 0) + 1)
                self._criterion_last_action_sequence[criterion_id] = fact.sequence
        changed = fact.activity_type in {"action", "retry"}
        if (fact.activity_type == "goal_progress" and fact.progress_class in {
                "GOAL_PROGRESS", "BLOCKER_REMOVED"}):
            for criterion_id in fact.affected_criterion_ids:
                self._criterion_retry_counts[criterion_id] = 0
            self._reset_progress_redirects()
            changed = True
        elif fact.artifact_changed is True:
            self._reset_progress_redirects()
            changed = True
        if changed:
            self._revision += 1

    def record_failed_approach(self, descriptor: AttemptSemanticDescriptor, *,
                               sequence: int) -> None:
        for criterion_id in descriptor.affected_criterion_ids:
            if criterion_id not in self._criterion_retry_counts:
                continue
            self._criterion_retry_counts[criterion_id] = (
                self._criterion_retry_counts.get(criterion_id, 0) + 1)
            self._criterion_last_action_sequence[criterion_id] = sequence
        self._revision += 1

    def record_progress_redirect(self, *, action_digest: str,
                                 criterion_id: str | None,
                                 criteria: Sequence[CriterionState],
                                 max_redirects_per_criterion: int = 2) -> str:
        """Redirect a repeated failed action, then yield to another criterion."""
        if (not isinstance(action_digest, str) or not action_digest
                or len(action_digest) > 128
                or not 1 <= max_redirects_per_criterion <= 8):
            raise ValueError("progress redirect identity or limit is invalid")
        criterion_ids = {item.criterion_id for item in criteria}
        selected_criterion = (criterion_id if criterion_id in criterion_ids
                              else next(iter(self.active_attention_window(criteria)), ""))
        key = selected_criterion or "__task__"
        if (self._last_progress_redirect_action == action_digest
                and self._last_progress_redirect_criterion == key):
            return "repeat"
        count = self._criterion_redirect_counts.get(key, 0)
        if count >= max_redirects_per_criterion:
            return "limit"
        count += 1
        self._criterion_redirect_counts[key] = count
        self._last_progress_redirect_action = action_digest
        self._last_progress_redirect_criterion = key
        self._revision += 1
        if count >= max_redirects_per_criterion:
            next_criteria = tuple(item for item in self.active_attention_window(criteria)
                                  if item != key)
            if next_criteria:
                self._progress_redirect_yield_target = next_criteria[0]
                self._progress_redirect_guidance_pending = True
                return "yield"
            return "limit"
        self._progress_redirect_guidance_pending = True
        return "redirect"

    def consume_progress_redirect_yield(self, criterion_id: str | None) -> bool:
        if (not criterion_id or criterion_id != self._progress_redirect_yield_target):
            return False
        self._progress_redirect_yield_target = None
        self._revision += 1
        return True

    def take_progress_redirect_guidance(self) -> str | None:
        if not self._progress_redirect_guidance_pending:
            return None
        self._progress_redirect_guidance_pending = False
        self._revision += 1
        return self._last_progress_redirect_criterion

    def _reset_progress_redirects(self) -> None:
        self._progress_redirect_epoch += 1
        self._criterion_redirect_counts.clear()
        self._last_progress_redirect_action = None
        self._last_progress_redirect_criterion = None
        self._progress_redirect_yield_target = None
        self._progress_redirect_guidance_pending = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "action_counts": dict(self._criterion_action_counts),
            "retry_counts": dict(self._criterion_retry_counts),
            "last_action_sequence": dict(self._criterion_last_action_sequence),
            "redirect_counts": dict(self._criterion_redirect_counts),
            "progress_redirect_epoch": self._progress_redirect_epoch,
            "last_progress_redirect_action": self._last_progress_redirect_action,
            "last_progress_redirect_criterion": self._last_progress_redirect_criterion,
            "progress_redirect_yield_target": self._progress_redirect_yield_target,
            "progress_redirect_guidance_pending": self._progress_redirect_guidance_pending,
            "revision": self._revision,
        }

    def restore(self, value: Mapping[str, Any], *,
                criteria: Sequence[CriterionState]) -> None:
        prior_fields = {"action_counts", "retry_counts", "last_action_sequence"}
        redirect_fields = prior_fields | {
            "redirect_counts", "progress_redirect_epoch",
            "last_progress_redirect_action", "last_progress_redirect_criterion",
            "progress_redirect_yield_target", "progress_redirect_guidance_pending",
        }
        current_fields = redirect_fields | {"revision"}
        if not isinstance(value, Mapping) or set(value) not in (
                prior_fields, redirect_fields, current_fields, set()):
            raise ValueError("malformed criterion attention snapshot")
        criterion_ids = {item.criterion_id for item in criteria}
        restored: dict[str, dict[str, int]] = {}
        for name in ("action_counts", "retry_counts", "last_action_sequence",
                     "redirect_counts"):
            source = value.get(name) or {}
            if (not isinstance(source, Mapping) or len(source) > 256
                    or not set(source).issubset(criterion_ids | {"__task__"})
                    or any(type(count) is not int or count < 0
                           for count in source.values())):
                raise ValueError("malformed criterion attention counters")
            restored[name] = {str(key): count for key, count in source.items()}
        self._criterion_action_counts = {
            key: restored["action_counts"].get(key, 0) for key in criterion_ids}
        self._criterion_retry_counts = {
            key: restored["retry_counts"].get(key, 0) for key in criterion_ids}
        self._criterion_last_action_sequence = {
            key: restored["last_action_sequence"].get(key, 0) for key in criterion_ids}
        self._criterion_redirect_counts = {
            key: restored["redirect_counts"].get(key, 0) for key in criterion_ids}
        if "__task__" in restored["redirect_counts"]:
            self._criterion_redirect_counts["__task__"] = restored[
                "redirect_counts"]["__task__"]
        epoch = value.get("progress_redirect_epoch", 0)
        if type(epoch) is not int or epoch < 0:
            raise ValueError("malformed progress redirect epoch")
        self._progress_redirect_epoch = epoch
        for name in ("last_progress_redirect_action",
                     "last_progress_redirect_criterion"):
            raw = value.get(name)
            if raw is not None and (not isinstance(raw, str) or len(raw) > 128):
                raise ValueError("malformed progress redirect identity")
        self._last_progress_redirect_action = value.get("last_progress_redirect_action")
        self._last_progress_redirect_criterion = value.get(
            "last_progress_redirect_criterion")
        yield_target = value.get("progress_redirect_yield_target")
        if yield_target is not None and (
                not isinstance(yield_target, str) or yield_target not in criterion_ids):
            raise ValueError("malformed progress redirect yield target")
        guidance_pending = value.get("progress_redirect_guidance_pending", False)
        if type(guidance_pending) is not bool:
            raise ValueError("malformed progress redirect guidance flag")
        revision = value.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise ValueError("malformed standalone attention revision")
        self._progress_redirect_yield_target = yield_target
        self._progress_redirect_guidance_pending = guidance_pending
        self._revision = revision


__all__ = ["StandaloneAttentionPolicy", "StandaloneAttentionState"]
