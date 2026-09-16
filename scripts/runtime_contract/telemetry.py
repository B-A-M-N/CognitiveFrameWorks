"""Canonical DP behavior-event schema contract for host telemetry."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, Tuple

def behavior_schema_contract(path: Path) -> Tuple[Dict[str, Any], str, str]:
    if not path.exists():
        candidates = [Path(__file__).resolve().parents[2] / "contracts" / path.name,
                      Path(__file__).resolve().parents[1] / "contracts" / path.name]
        for local in candidates:
            if local.exists():
                path = local
                break
    if not path.exists():
        raise ValueError(f"behavior-event schema is unavailable: {path}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    version = str(schema.get("properties", {}).get("schema_version", {}).get("const", ""))
    return schema, hashlib.sha256(path.read_bytes()).hexdigest(), version
