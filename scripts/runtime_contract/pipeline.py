"""Pipeline manifest loading and normalization boundary."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


def load_pipeline_document(path: Path) -> Dict[str, Any]:
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    stages = {item["id"]: item for item in data.get("stages", [])}
    return {
        "stages": stages,
        "aliases": data.get("aliases", {}) or {},
        "profiles": data.get("profiles", {}) or {},
    }
