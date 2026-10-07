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
            if project.name == "CognitiveFrameWorks":
                metadata_name = next(name for name in names
                                     if name.endswith(".dist-info/METADATA"))
                metadata = archive.read(metadata_name).decode("utf-8")
                if ("cognitive-runtime-codex-adapter" in metadata
                        or "cognitive-frameworks-codex-adapter" in metadata):
                    raise RuntimeError("base CFW wheel depends on a Codex adapter")
        install_root = out / "installed-wheel"
        dependency_wheels: list[Path] = []
        if project.name == "DigitalPsychology":
            # DP's application wheel now depends on the independent runtime-core
            # distribution. Stage both wheels so this isolated import check
            # exercises the declared package boundary without a package index.
            core_project = staged / "cognitive_runtime_core"
            core_out = out / "runtime-core-wheel"
            core_out.mkdir()
            core_result = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps",
                 "--no-build-isolation", "--no-cache-dir", "-w",
                 str(core_out), str(core_project)],
                cwd=core_project, env=env, text=True, capture_output=True,
                check=False)
            if core_result.returncode:
                raise RuntimeError(core_result.stdout + core_result.stderr)
            dependency_wheels.append(next(core_out.glob("*.whl")))
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-index",
                        "--no-deps", "--target", str(install_root),
                        str(wheel), *(str(path) for path in dependency_wheels)],
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
                "assert FlowSelector; "
                "from cognitive_statework.workflow_planner import WorkflowPlanner; "
                "assert WorkflowPlanner; "
                "from cognitive_statework.cfw_authority import CFWAuthorityAdapter; "
                "assert CFWAuthorityAdapter; "
                "from cognitive_statework.cfw_provider import CognitiveStateWorkProvider; "
                "assert callable(CognitiveStateWorkProvider.create_state_authority)"
            ),
            # A base CFW wheel intentionally omits the optional harness-runtime
            # dependencies, so Codex adapter imports belong in adapter tests.
            "CognitiveFrameWorks": (
                "import importlib.util; "
                "from runtime_contract.routing import _profile_schema_text; "
                "assert 'behavioral-routing-profile' in _profile_schema_text(); "
                "from runtime_contract.component_control import COMPONENT_IDS; "
                "assert {'owl', 'anchor', 'fuse', 'ward', 'flow', 'sispis', 'dox'} == set(COMPONENT_IDS); "
                "from runtime_contract.composite_snapshot import CFWPolicySnapshot, CompositeCognitiveRuntimeSnapshot; "
                "assert CFWPolicySnapshot and CompositeCognitiveRuntimeSnapshot; "
                "from runtime_start.validation import validate_start_contracts; "
                "from runtime_start.initialization import initialize_runtime_session; "
                "assert importlib.util.find_spec('codex_host') is None; "
                "assert importlib.util.find_spec('codex_app_server_adapter') is None; "
                "assert importlib.util.find_spec('codex_app_server_dp_bridge') is None; "
                "assert validate_start_contracts and initialize_runtime_session"
            ),
        }
        import_result = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); " + checks[project.name],
             str(install_root)],
            cwd=out, check=False, capture_output=True, text=True)
        if import_result.returncode:
            raise RuntimeError(import_result.stdout + import_result.stderr)
        if project.name == "CognitiveFrameWorks":
            adapter_project = staged / "integrations" / "cfw-codex-adapter"
            adapter_out = out / "cfw-codex-adapter-wheel"
            adapter_out.mkdir()
            adapter_result = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps",
                 "--no-build-isolation", "--no-cache-dir", "-w",
                 str(adapter_out), str(adapter_project)],
                cwd=staged, env=env, text=True, capture_output=True, check=False)
            if adapter_result.returncode:
                raise RuntimeError(adapter_result.stdout + adapter_result.stderr)
            adapter_wheel = next(adapter_out.glob("*.whl"))
            with zipfile.ZipFile(adapter_wheel) as archive:
                adapter_names = set(archive.namelist())
                expected_adapter_files = {
                    "codex_host/adapter.py",
                    "codex_host/client.py",
                    "codex_app_server_adapter.py",
                    "codex_app_server_dp_bridge.py",
                }
                if not expected_adapter_files.issubset(adapter_names):
                    raise RuntimeError(
                        "CFW Codex adapter wheel is missing files: "
                        + ", ".join(sorted(expected_adapter_files - adapter_names)))
                metadata_name = next(name for name in adapter_names
                                     if name.endswith(".dist-info/METADATA"))
                metadata = archive.read(metadata_name).decode("utf-8")
                for requirement in (
                        "Requires-Dist: cognitive-frameworks-runtime",
                        "Requires-Dist: cognitive-runtime-core",
                        "Requires-Dist: cognitive-runtime-codex-adapter"):
                    if requirement not in metadata:
                        raise RuntimeError(
                            f"CFW Codex adapter metadata is missing {requirement}")
                if any(name.startswith("codex_host/") for name in names):
                    raise RuntimeError("base CFW wheel still contains codex_host")
                if {"codex_app_server_adapter.py",
                    "codex_app_server_dp_bridge.py"} & names:
                    raise RuntimeError("base CFW wheel still contains Codex facades")

            codex_project = (ROOT.parent / "DigitalPsychology" /
                             "cognitive_runtime_core" / "integrations" / "codex")
            staged_codex = out / "runtime-codex-adapter"
            shutil.copytree(
                codex_project,
                staged_codex,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", "*.pyc", "*.egg-info",
                    ".pytest_cache", ".venv", "venv", "build", "dist"),
            )
            codex_out = out / "runtime-codex-adapter-wheel"
            codex_out.mkdir()
            codex_result = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps",
                 "--no-build-isolation", "--no-cache-dir", "-w",
                 str(codex_out), str(staged_codex)],
                cwd=staged_codex, env=env, text=True, capture_output=True,
                check=False)
            if codex_result.returncode:
                raise RuntimeError(codex_result.stdout + codex_result.stderr)
            codex_wheel = next(codex_out.glob("*.whl"))
            with zipfile.ZipFile(codex_wheel) as archive:
                codex_names = set(archive.namelist())
                expected_cfw_host = {
                    "cognitive_runtime_core/integrations/codex/cfw_host/adapter.py",
                    "cognitive_runtime_core/integrations/codex/cfw_host/actions.py",
                    "cognitive_runtime_core/integrations/codex/cfw_host/completion.py",
                    "cognitive_runtime_core/integrations/codex/cfw_host/validators.py",
                }
                if not expected_cfw_host.issubset(codex_names):
                    raise RuntimeError(
                        "runtime Codex adapter wheel is missing CFW host files: "
                        + ", ".join(sorted(expected_cfw_host - codex_names)))
                metadata_name = next(name for name in codex_names
                                     if name.endswith(".dist-info/METADATA"))
                metadata = archive.read(metadata_name).decode("utf-8")
                if ("Provides-Extra: cfw" not in metadata
                        or 'Requires-Dist: cognitive-frameworks-runtime' not in metadata):
                    raise RuntimeError(
                        "runtime Codex adapter wheel is missing its optional CFW extra")
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
    assert "cognitive_statework/workflow_planner.py" in csw
    assert "cognitive_statework/cfw_authority.py" in csw
    assert "runtime_contract/contracts/behavioral-routing-profile.schema.json" in cfw
    assert "runtime_contract/contracts/runtime-event.schema.json" in cfw
    assert "runtime_contract/resources/contracts/runtime-event.schema.json" in cfw
    assert "runtime_contract/contracts/routing-experiment-plan.schema.json" in cfw
    assert "runtime_contract/pipeline.yaml" in cfw
    assert "runtime_contract/workflow_port.py" in cfw
    assert "runtime_start/validation.py" in cfw
    assert "runtime_start/initialization.py" in cfw
    print("PACKAGE_RESOURCE_TEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
