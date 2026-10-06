"""Host-side validator scoring for baseline and treatment runs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .fixture import _source_digest
from .validators import VALIDATOR_FILES, _run_validator

def _run_baseline_validator_set(workspace: Path, *,
                                validation_runs: list[dict[str, Any]],
                                channel: str) -> list[dict[str, Any]]:
    results = []
    source_digest = _source_digest(workspace)
    for validator_id in VALIDATOR_FILES:
        result = _run_validator(workspace, validator_id)
        validation_runs.append({"channel": channel, **result,
                                "source_sha256": source_digest})
        results.append(result)
    return results
