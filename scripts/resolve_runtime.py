"""Compatibility imports for the public :mod:`resolver.api` surface."""
from __future__ import annotations

from resolver import api as _api


def __getattr__(name: str):
    return getattr(_api, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_api)))
