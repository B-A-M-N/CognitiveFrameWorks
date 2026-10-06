"""Shared disposable evaluation paths and import setup."""
from __future__ import annotations

import os
from pathlib import Path
import sys

CFW = Path(__file__).resolve().parents[2]
CSW = Path(os.environ.get("CSW_ROOT", CFW.parent / "CognitiveStateWork")).resolve()
DP = Path(os.environ.get("DP_ROOT", CFW.parent / "DigitalPsychology")).resolve()
os.environ.setdefault("COGNITIVE_STATEWORK_ROOT", str(CSW))
sys.path[:0] = [str(CFW / "scripts"), str(CSW / "scripts"), str(DP)]
