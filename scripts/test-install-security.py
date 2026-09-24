#!/usr/bin/env python3
"""Hostile installer fixtures: symlink escapes and ownership collisions."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATEWORK = ROOT.parent / "CognitiveStateWork"


def run(registry: Path) -> int:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "install-framework.py"),
        "--registry", f"hostile={registry}", "--statework-root", str(STATEWORK)],
        cwd=ROOT, env=env,
        text=True, capture_output=True, check=False)
    return result.returncode


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="installer-security-") as raw:
        root = Path(raw)
        registry = root / "registry"
        registry.mkdir()
        external = root / "external"
        external.mkdir()
        sentinel = external / "SKILL.md"
        sentinel.write_text("sentinel\n", encoding="utf-8")
        (registry / "OWL").symlink_to(external, target_is_directory=True)
        assert run(registry) != 0
        assert sentinel.read_text(encoding="utf-8") == "sentinel\n"
        registry.unlink() if registry.is_symlink() else None
    print("INSTALL_SECURITY_TEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
