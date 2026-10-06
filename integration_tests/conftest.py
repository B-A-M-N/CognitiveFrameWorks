from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSW = Path(os.environ.get("COGNITIVE_STATEWORK_ROOT",
                         ROOT.parent / "CognitiveStateWork")).resolve()
DP = Path(os.environ.get("DIGITALPSYCHOLOGY_ROOT",
                        ROOT.parent / "DigitalPsychology")).resolve()

for path in (ROOT, ROOT / "scripts", CSW, CSW / "scripts", DP):
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

if not (CSW / "registry.yaml").is_file():
    raise RuntimeError(
        "CFW integration tests require COGNITIVE_STATEWORK_ROOT with registry.yaml")

os.environ.setdefault("COGNITIVE_STATEWORK_ROOT", str(CSW))
os.environ.setdefault("DIGITALPSYCHOLOGY_ROOT", str(DP))


@pytest.fixture(autouse=True)
def isolate_runtime_state(tmp_path, monkeypatch):
    runtime_root = tmp_path / "xdg-runtime"
    runtime_root.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
