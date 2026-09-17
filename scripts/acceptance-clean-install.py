#!/usr/bin/env python3
"""Public-beta gate for a self-contained Cognitive Runtime installation."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATEWORK = ROOT.parent / "CognitiveStateWork"
DP = ROOT.parent / "DigitalPsychology"


def run(command: list[str], *, env: dict[str, str]) -> None:
    result = subprocess.run(command, cwd=ROOT, env=env,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).strip())


def main() -> int:
    with (tempfile.TemporaryDirectory(prefix="cfw-beta-home-") as home,
          tempfile.TemporaryDirectory(prefix="cfw-beta-runtime-") as runtime_dir):
        env = dict(os.environ)
        env.update({"HOME": home, "XDG_RUNTIME_DIR": runtime_dir})
        custom_registry = str(Path(home) / ".example-host" / "skills")
        run([sys.executable, str(ROOT / "scripts" / "install-framework.py"),
             "--registry", f"example-host={custom_registry}",
             "--statework-root", str(STATEWORK),
             "--digital-psychology-root", str(DP)], env=env)
        run([sys.executable, str(ROOT / "scripts" / "doctor.py"),
             "--registry", f"example-host={custom_registry}"], env=env)
    print("INSTALLATION_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
