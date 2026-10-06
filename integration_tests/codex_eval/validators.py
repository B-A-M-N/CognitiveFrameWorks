"""Fixture host validators and CFW completion-bound validator contracts."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from codex_app_server_adapter import CodexValidatorBinding


VALIDATOR_FILES = {
    "parser": "tests/test_parser.py",
    "retry": "tests/test_retry.py",
    "state": "tests/test_state.py",
    "integration": "tests/test_integration.py",
}

COMPLETION_RESULT_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "status": {"const": "passed"},
        "validator": {"type": "string"},
        "tests_passed": {"type": "integer", "minimum": 0},
        "tests_failed": {"const": 0},
        "summary": {"type": "string", "maxLength": 1024},
        "elapsed_ms": {"type": "number", "minimum": 0},
    },
    "required": ["status", "validator", "tests_passed", "tests_failed", "summary"],
    "additionalProperties": False,
}


def _run_validator(workspace: Path, validator_id: str) -> dict[str, Any]:
    test_file = VALIDATOR_FILES.get(validator_id)
    if test_file is None:
        raise ValueError("unknown host validator")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_file],
        cwd=workspace, env=env, capture_output=True, text=True,
        timeout=90, check=False,
    )
    output = (completed.stdout + completed.stderr)[-1200:]
    passed_match = re.search(r"(\d+) passed", output)
    failed_match = re.search(r"(\d+) failed", output)
    errors_match = re.search(r"(\d+) error", output)
    failures = int(failed_match.group(1)) if failed_match else 0
    errors = int(errors_match.group(1)) if errors_match else 0
    if completed.returncode != 0 and failures == 0 and errors == 0:
        failures = 1
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "validator": validator_id,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "tests_passed": int(passed_match.group(1)) if passed_match else 0,
        "tests_failed": failures + errors,
        "summary": output.strip()[-1024:],
    }

def _validator_definitions(contract: Mapping[str, Any]) -> tuple[
        tuple[CodexValidatorBinding, ...], dict[str, str]]:
    bindings = []
    criterion_by_validator = {}
    for criterion in contract["criteria"]:
        validator_id = str(criterion["validator"])
        criterion_id = str(criterion["id"])
        criterion_by_validator[validator_id] = criterion_id
        bindings.append(CodexValidatorBinding(
            validator_id=f"coding-{validator_id}-v1",
            action_binding_id="host_validator",
            arguments={"validator": validator_id},
            criterion_ids=(criterion_id,),
            covered_boundary_ids=(f"validator:{validator_id}",),
            result_schema={**COMPLETION_RESULT_SCHEMA_BASE,
                           "properties": {
                               **COMPLETION_RESULT_SCHEMA_BASE["properties"],
                               "validator": {"const": validator_id},
                           }},
        ))
    return tuple(bindings), criterion_by_validator
