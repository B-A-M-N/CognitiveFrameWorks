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
        custom_registry = str(home_path / ".example-host" / "skills")
        installer = [sys.executable, str(ROOT / "scripts" / "install-framework.py"),
                     "--registry", f"example-host={custom_registry}",
                     "--statework-root", str(STATEWORK)]
        doctor = [sys.executable, str(ROOT / "scripts" / "doctor.py"),
                  "--registry", f"example-host={custom_registry}"]
        run(installer, env)
        run(doctor, env)
        runtime = home_path / ".example-host" / "skills" / "cognitiveframeworks_runtime"
        registry = home_path / ".example-host" / "skills"
        sentinels = {
            registry / "OWL" / "references" / "local-reference.md": "local reference",
            registry / "OWL" / "examples" / "local-example.txt": "local example",
            registry / "OWL" / "adapters" / "local-adapter.txt": "local adapter",
            registry / "OWL" / "local-extension.txt": "local extension",
            registry / "gitter" / "local-statework-extension.txt": "local statework extension",
        }
        for path, contents in sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        stale = runtime / "stale-beta-file.txt"
        stale.write_text("old beta", encoding="utf-8")
        run(installer, env)
        run(doctor, env)
        if stale.exists():
            raise RuntimeError("upgrade retained stale beta runtime content")
        for path, contents in sentinels.items():
            if not path.exists() or path.read_text(encoding="utf-8") != contents:
                raise RuntimeError(f"upgrade overwrote unowned file {path}")
        manifest = registry / ".cognitiveframeworks-cfw-managed.json"
        if not manifest.exists():
            raise RuntimeError("installer did not publish ownership manifest")
        managed = __import__("json").loads(manifest.read_text(encoding="utf-8"))["managed_paths"]
        if "OWL/SKILL.md" not in managed or "cognitiveframeworks_runtime" not in managed:
            raise RuntimeError("ownership manifest omitted managed installation paths")
        if any(path.relative_to(registry).as_posix() in managed for path in sentinels):
            raise RuntimeError("ownership manifest claimed an unowned sentinel")
    print("UPGRADE_ACCEPTANCE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
