"""Importable CFW resolver API and its focused implementation modules."""
from __future__ import annotations

from importlib import import_module

_PUBLIC = {"AcceptanceCriterion", "RuntimeBundle", "SnapshotIntegrityError",
           "SystemOnePolicy", "TaskGoalContract", "TaskRequest", "resolve"}


def __getattr__(name: str):
    if name not in _PUBLIC:
        raise AttributeError(name)
    return getattr(import_module(".api", __name__), name)


__all__ = sorted(_PUBLIC)
