#!/usr/bin/env python3
"""Acceptance harness for an irreducibly cognitive prompt intervention."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="cfw-prompt-acceptance-")
    resolver = load("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
    prompt_guard = {
        "id": "G-prompt-acceptance", "key": "G-prompt-acceptance@1",
        "family": "G-prompt-acceptance", "version": "1", "status": "active",
        "target_behavior": "cognitive ambiguity", "rule": "State the uncertainty and inspect the missing premise before answering.",
        "priority": "P1", "enforcement_key": "prompt.cognitive_ambiguity",
        "enforcement": {"mode": "prompt", "handler": None, "handler_version": "1",
                         "parameters": {}, "fallback_prompt":
                         "State the uncertainty and inspect the missing premise before answering."},
    }
    original_loader = resolver.load_guard_pack
    resolver.load_guard_pack = lambda validate=True: {
        **original_loader(validate), "guards": [prompt_guard],
    }
    runtime = load("runtime", ROOT / "scripts" / "runtime.py")
    bundle = resolver.resolve("prompt-acceptance", "quick")
    session = runtime.start(bundle, telemetry_disabled=True)
    prompt = session.before_model_call()
    fake_model_response = ("uncertainty acknowledged"
                           if prompt_guard["rule"] in prompt else
                           "answered without inspecting")
    assert fake_model_response == "uncertainty acknowledged"
    print("PROMPT_ACCEPTANCE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
