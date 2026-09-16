"""Immutable policy-contract helpers shared by resolver-facing code."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def freeze_contracts(contracts: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a recursively immutable contract mapping."""
    from types import MappingProxyType
    def freeze(value: Any) -> Any:
        if isinstance(value, dict):
            return MappingProxyType({key: freeze(item) for key, item in value.items()})
        if isinstance(value, list):
            return tuple(freeze(item) for item in value)
        return value
    return freeze(dict(contracts))
