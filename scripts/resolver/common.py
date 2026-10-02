"""Small pure helpers shared by CFW resolver components."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any
from collections.abc import Mapping


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dp_trusted_guard_pack_path() -> Path:
    """The only DP deployment artifact CFW consumes."""
    if os.environ.get("DIGITALPSYCHOLOGY_STATE_ROOT"):
        return Path(os.environ["DIGITALPSYCHOLOGY_STATE_ROOT"]) / "compiled-guard-pack.json"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "digitalpsychology" / "compiled-guard-pack.json"
def load_yaml(path: Path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())
def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
def _frozen_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen_mapping(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen_mapping(item) for item in value)
    return value
def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
