#!/usr/bin/env python3
"""Upgrade acceptance from a stale beta installation in an empty temp HOME."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATEWORK = ROOT.parent / "CognitiveStateWork"
DP = ROOT.parent / "DigitalPsychology"


def run(command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(command, cwd=ROOT, env=env,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).strip())


def main() -> int:
    with (tempfile.TemporaryDirectory(prefix="cfw-upgrade-home-") as home,
          tempfile.TemporaryDirectory(prefix="cfw-upgrade-xdg-") as xdg):
        home_path = Path(home)
        if any(home_path.iterdir()):
            raise RuntimeError("upgrade HOME was not empty")
        env = dict(os.environ)
        env.update({"HOME": home, "XDG_RUNTIME_DIR": xdg})
        installer = [sys.executable, str(ROOT / "scripts" / "install-framework.py"),
                     "--target", "harvardcodex", "--statework-root", str(STATEWORK),
                     "--digital-psychology-root", str(DP)]
        doctor = [sys.executable, str(ROOT / "scripts" / "doctor.py"),
                  "--target", "harvardcodex"]
        run(installer, env)
        run(doctor, env)
        runtime = home_path / ".harvardcodex" / "skills" / "cognitiveframeworks_runtime"
        stale = runtime / "stale-beta-file.txt"
        stale.write_text("old beta", encoding="utf-8")
        run(installer, env)
        run(doctor, env)
        if stale.exists():
            raise RuntimeError("upgrade retained stale beta runtime content")
    print("UPGRADE_ACCEPTANCE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
