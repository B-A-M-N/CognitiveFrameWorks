#!/usr/bin/env python3
"""Run CFW's cross-repository integration suite with the CSW provider installed."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSW = Path(os.environ.get(
    "COGNITIVE_STATEWORK_ROOT", ROOT.parent / "CognitiveStateWork")).resolve()


def main() -> int:
    if not (CSW / "registry.yaml").is_file():
        print("CFW integration tests require COGNITIVE_STATEWORK_ROOT with registry.yaml",
              file=sys.stderr)
        return 2
    if importlib.util.find_spec("pytest") is None:
        print("pytest is required to run integration_tests", file=sys.stderr)
        return 2
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "integration_tests", *sys.argv[1:]],
        cwd=ROOT,
        env={**os.environ, "COGNITIVE_STATEWORK_ROOT": str(CSW)},
        check=False,
    )
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
