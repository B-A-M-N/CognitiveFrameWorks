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
    if project.resolve() == ROOT.resolve():
        source_manifest = project / "shared" / "pipeline.yaml"
        package_manifest = project / "scripts" / "runtime_contract" / "pipeline.yaml"
        if source_manifest.read_bytes() != package_manifest.read_bytes():
            raise RuntimeError("packaged CFW pipeline differs from shared/pipeline.yaml")
    with tempfile.TemporaryDirectory(prefix="wheel-resource-") as raw:
        out = Path(raw)
        staged = out / project.name
        shutil.copytree(
            project,
            staged,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", "*.egg-info", ".pytest_cache",
                ".venv", "venv", "build", "dist",
            ),
        )
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
        install_root = out / "installed-wheel"
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-index",
                        "--no-deps", "--target", str(install_root), str(wheel)],
                       check=True, capture_output=True, text=True)
        checks = {
            "DigitalPsychology": (
                "from digital_psychology.routing_profiles import _schema; "
                "assert _schema()['$id']"
            ),
            "CognitiveStateWork": (
                "from importlib.resources import files; "
                "root = files('cognitive_statework'); "
                "assert root.joinpath('schemas/packet-registry.yaml').is_file(); "
                "assert root.joinpath('compiled-registry.json').is_file(); "
                "assert root.joinpath('tuid/manifest.yaml').is_file(); "
                "from cognitive_statework.flow_selectors import FlowSelector; "
                "assert FlowSelector"
            ),
            "CognitiveFrameWorks": (
                "from runtime_contract.routing import _profile_schema_text; "
                "assert 'behavioral-routing-profile' in _profile_schema_text(); "
                "from runtime_contract.component_control import COMPONENT_IDS; "
                "assert {'owl', 'anchor', 'fuse', 'ward', 'flow', 'sispis', 'dox'} == set(COMPONENT_IDS)"
            ),
        }
        import_result = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); " + checks[project.name],
             str(install_root)],
            cwd=out, check=False, capture_output=True, text=True)
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
    assert "cognitive_statework/compiled-registry.json" in csw
    assert "cognitive_statework/tuid/manifest.yaml" in csw
    assert "cognitive_statework/flow_selectors.py" in csw
    assert "runtime_contract/contracts/behavioral-routing-profile.schema.json" in cfw
    assert "runtime_contract/contracts/routing-experiment-plan.schema.json" in cfw
    assert "runtime_contract/pipeline.yaml" in cfw
    print("PACKAGE_RESOURCE_TEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
