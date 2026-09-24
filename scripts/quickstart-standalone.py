#!/usr/bin/env python3
"""Five-minute CFW-only example with no CSW or DP checkout."""
from __future__ import annotations
import importlib.util, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

resolve = load("quickstart_resolve", ROOT / "scripts/resolve-runtime.py")
load("quickstart_runtime", ROOT / "scripts/runtime.py")
api = load("quickstart_api", ROOT / "scripts/cognitive_runtime.py")

with tempfile.TemporaryDirectory(prefix="cfw-quickstart-") as raw:
    resolve.runtime_state_dir = lambda: Path(raw)
    request = resolve.TaskRequest(task_id="quickstart", application_id="readme-demo", shape="implement")
    runtime = api.CognitiveRuntime(telemetry_disabled=True)
    handle = runtime.begin_task(request)
    agent = runtime.agent_handle(handle)
    decision = agent.request_action({"tool_binding_id": "read", "arguments": {}})
    print("kernel:", " -> ".join(handle.bundle.kernel))
    print("read action gate:", decision.value)
    print("policy snapshot:", handle.bundle.pinned["policy_hash"])
