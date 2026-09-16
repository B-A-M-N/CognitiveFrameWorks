"""Fail-closed compatibility checks for vendored runtime contracts."""
from __future__ import annotations

from typing import Any


def _major(version: Any) -> int:
    try:
        return int(str(version).split(".", 1)[0])
    except (TypeError, ValueError):
        return -1


def validate_compatibility(*, statework_registry_version: Any,
                            behavior_event_version: Any,
                            guard_pack_version: Any) -> None:
    expected = {
        "StateWork": (statework_registry_version, 1),
        "behavior-event": (behavior_event_version, 2),
        "guard-pack": (guard_pack_version, 1),
    }
    failures = [f"{name}={version!r} (expected major {major})"
                for name, (version, major) in expected.items()
                if _major(version) != major]
    if failures:
        raise ValueError("incompatible runtime contract(s): " + ", ".join(failures))
