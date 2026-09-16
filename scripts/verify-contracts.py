#!/usr/bin/env python3
"""Verify that runtime-vendored contracts match their source contracts."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COW = ROOT.parent / "CognitiveStateWork"
DP = ROOT.parent / "DigitalPsychology"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


PAIRS = (
    (ROOT / "contracts" / "behavior-event.schema.json", DP / "schemas" / "behavior-event.schema.json"),
    (ROOT / "contracts" / "guard.schema.json", DP / "schemas" / "guard.schema.json"),
    (ROOT / "contracts" / "guard-pack.schema.json", DP / "schemas" / "guard-pack.schema.json"),
    (ROOT / "contracts" / "statework-manifest.schema.json", COW / "schemas" / "statework-manifest.schema.json"),
    (ROOT / "contracts" / "transition-contract.schema.json", COW / "schemas" / "transition-contract.schema.json"),
)


def main() -> int:
    failures = []
    for runtime, source in PAIRS:
        if not runtime.exists() or not source.exists():
            failures.append(f"missing contract: {runtime} or {source}")
            continue
        if digest(runtime) != digest(source):
            failures.append(f"contract drift: {runtime.relative_to(ROOT)} != {source}")
        else:
            print(f"[ok] {runtime.name}")
    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1
    try:
        routing_runtime = json.loads((ROOT / "contracts" / "behavioral-routing-profile.schema.json").read_text(encoding="utf-8"))
        routing_source = json.loads((DP / "schemas" / "behavioral-routing-profile.schema.json").read_text(encoding="utf-8"))
        if routing_runtime != routing_source:
            print("[FAIL] behavioral-routing-profile.schema.json semantic drift")
            return 1
        print("[ok] behavioral-routing-profile.schema.json")
        from runtime_contract.handlers import handler_capabilities
        artifact = json.loads(
            (ROOT / "contracts" / "enforcement-capabilities.json").read_text(
                encoding="utf-8"))
        expected = handler_capabilities()
        actual = artifact.get("handlers")
        comparable = {
            key: {field: value for field, value in data.items() if field != "handler"}
            for key, data in expected.items()
        }
        if actual != comparable:
            print("[FAIL] enforcement-capabilities.json drift from handler registry")
            return 1
        print("[ok] enforcement-capabilities.json")
    except Exception as exc:
        print(f"[FAIL] enforcement capability verification failed: {exc}")
        return 1
    print("contract parity passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
