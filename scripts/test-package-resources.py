#!/usr/bin/env python3
"""Build wheels and assert runtime contract resources are packaged."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def wheel_files(project: Path) -> set[str]:
    with tempfile.TemporaryDirectory(prefix="wheel-resource-") as raw:
        out = Path(raw)
        staged = out / project.name
        shutil.copytree(project, staged,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                                "--no-build-isolation", "--no-cache-dir", "-w",
                                str(out), str(staged)],
                               cwd=staged, env=env, text=True,
                               capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        wheel = next(out.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            assert not any(name.endswith(".pyc") or "/__pycache__/" in name for name in names)
        venv = out / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
                       check=True, capture_output=True, text=True)
        venv_python = venv / "bin" / "python"
        subprocess.run([str(venv_python), "-m", "pip", "install", "--no-index",
                        "--no-deps", str(wheel)], check=True, capture_output=True, text=True)
        checks = {
            "DigitalPsychology": (
                "from digital_psychology.routing_profiles import _schema; "
                "assert _schema()['$id']"
            ),
            "CognitiveStateWork": (
                "from importlib.resources import files; "
                "assert files('cognitive_statework').joinpath('schemas/packet-registry.yaml').is_file()"
            ),
            "CognitiveFrameWorks": (
                "from runtime_contract.routing import _profile_schema_text; "
                "assert 'behavioral-routing-profile' in _profile_schema_text()"
            ),
        }
        import_result = subprocess.run(
            [str(venv_python), "-I", "-c", checks[project.name]],
            cwd=venv, check=False, capture_output=True, text=True)
        if import_result.returncode:
            raise RuntimeError(import_result.stdout + import_result.stderr)
        return names


def main() -> int:
    dp = wheel_files(ROOT.parent / "DigitalPsychology")
    csw = wheel_files(ROOT.parent / "CognitiveStateWork")
    cfw = wheel_files(ROOT)
    assert "digital_psychology/schemas/behavior-event.schema.json" in dp
    assert "digital_psychology/schemas/routing-experiment-plan.schema.json" in dp
    assert "cognitive_statework/schemas/packet-registry.yaml" in csw
    assert "cognitive_statework/schemas/evidence-kinds.yaml" in csw
    assert "runtime_contract/contracts/behavioral-routing-profile.schema.json" in cfw
    assert "runtime_contract/contracts/routing-experiment-plan.schema.json" in cfw
    print("PACKAGE_RESOURCE_TEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
