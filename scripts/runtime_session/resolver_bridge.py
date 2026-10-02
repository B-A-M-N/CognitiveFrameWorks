"""Single cached bridge to the CFW resolver compatibility module."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]

def load_resolver() -> ModuleType:
    existing = sys.modules.get("resolve_runtime")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
    if spec is None or spec.loader is None:
        raise ImportError("CFW resolver module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules["resolve_runtime"] = module
    spec.loader.exec_module(module)
    return module

resolve_runtime = load_resolver()
