#!/usr/bin/env python3
"""Aggregate the gates required by the dynamic behavioral-runtime claim."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DP = ROOT.parent / "DigitalPsychology"


GATES = (
    ("INSTALLATION_GATE", ROOT / "scripts" / "acceptance-clean-install.py", ROOT),
    ("RUNTIME_CONTROL_PLANE_GATE", ROOT / "scripts" / "test-runtime.py", ROOT),
    ("SESSION_CORRELATION_GATE", DP / "scripts" / "test-routing-learning.py", DP),
    ("BEHAVIORAL_ADAPTATION_GATE", ROOT / "scripts" / "acceptance-session-routing.py", ROOT),
)


def main() -> int:
    for name, script, cwd in GATES:
        result = subprocess.run([sys.executable, str(script)], cwd=cwd,
                                text=True, check=False)
        if result.returncode:
            print(f"{name}=FAIL")
            return result.returncode
        print(f"{name}=PASS")
    print("PUBLIC_BETA_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
