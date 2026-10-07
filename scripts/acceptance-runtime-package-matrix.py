#!/usr/bin/env python3
"""Build isolated wheels and verify supported runtime package combinations."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
CSW_ROOT = Path(os.environ.get("CSW_ROOT", ROOT.parent / "CognitiveStateWork"))
DP_ROOT = Path(os.environ.get("DP_ROOT", ROOT.parent / "DigitalPsychology"))
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "build", "dist", ".pytest_cache")


def _run(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> str:
    completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                               text=True, timeout=180, check=False)
    if completed.returncode:
        raise RuntimeError((completed.stdout + completed.stderr).strip())
    return completed.stdout


def _mapped_source(project: Path, package: str,
                   package_dirs: dict[str, str]) -> tuple[Path, Path]:
    prefixes = [key for key in package_dirs
                if key == "" or package == key or package.startswith(key + ".")]
    prefix = max(prefixes, key=len) if prefixes else ""
    suffix = package[len(prefix):].lstrip(".").replace(".", os.sep)
    source = project / package_dirs.get(prefix, "") / suffix
    target = Path(package_dirs.get(prefix, "")) / suffix
    return source, target


def _copy_project(project: Path, destination: Path) -> None:
    metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = metadata.get("tool", {}).get("setuptools", {})
    package_dirs = setuptools.get("package-dir", {})
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project / "pyproject.toml", destination / "pyproject.toml")
    if (project / "README.md").is_file():
        shutil.copy2(project / "README.md", destination / "README.md")

    copied: set[tuple[Path, Path]] = set()
    for package in setuptools.get("packages", ()):
        source, target = _mapped_source(project, package, package_dirs)
        if not source.is_dir():
            raise FileNotFoundError(f"packaged module {package!r} is missing: {source}")
        key = (source.resolve(), target)
        if key in copied:
            continue
        copied.add(key)
        target_path = destination / target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target_path, dirs_exist_ok=True, ignore=IGNORE)

    default_source = project / package_dirs.get("", "")
    for module in setuptools.get("py-modules", ()):
        source = default_source / f"{module}.py"
        if not source.is_file():
            raise FileNotFoundError(f"packaged module {module!r} is missing: {source}")
        target = destination / package_dirs.get("", "") / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _build_wheel(name: str, source: Path, scratch: Path,
                 env: dict[str, str]) -> Path:
    project = scratch / "sources" / name
    _copy_project(source, project)
    wheel_dir = scratch / "wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    before = set(wheel_dir.glob("*.whl"))
    _run([sys.executable, "-m", "pip", "wheel", "--no-deps",
          "--no-build-isolation", "--wheel-dir", str(wheel_dir), str(project)], env=env)
    built = sorted(set(wheel_dir.glob("*.whl")) - before)
    if len(built) != 1:
        raise RuntimeError(f"expected one {name} wheel, found {len(built)}")
    return built[0]


def _exercise_cell(name: str, wheels: list[Path], imports: tuple[str, ...], *,
                   forbidden: tuple[str, ...], scratch: Path,
                   env: dict[str, str], start_cfw: bool = False,
                   start_csw: bool = False, start_dp: bool = False) -> None:
    target = scratch / "cells" / name
    target.mkdir(parents=True)
    _run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
          "--no-compile", "--target", str(target), *(str(item) for item in wheels)], env=env)
    code = f"""
import importlib, sys
for module in {imports!r}:
    importlib.import_module(module)
for root in {forbidden!r}:
    assert not any(name == root or name.startswith(root + '.') for name in sys.modules), root
"""
    cell_env = dict(env, PYTHONPATH=str(target))
    if start_cfw:
        code += f"""
from pathlib import Path
from resolve_runtime import resolve
from runtime import start
empty_stateworks = Path({str(scratch / 'empty-stateworks')!r})
empty_stateworks.mkdir(exist_ok=True)
bundle = resolve('wheel-matrix-cfw', shape='quick', domains=(), cow_root=empty_stateworks)
session = start(bundle, telemetry_disabled=True)
assert session.bundle.task_id == 'wheel-matrix-cfw'
close = getattr(session, 'close', None)
if callable(close): close()
"""
    if start_csw:
        code += """
from cognitive_statework.control_plane import open_compiled_registry
from cognitive_statework.registry import RegistryCompiler
RegistryCompiler().compile(write=True)
assert open_compiled_registry(verify_bundles=False) is not None
"""
    if start_dp:
        code += """
from digital_psychology.runtime.host import RuntimeHost
host = RuntimeHost()
assert host.service is None
"""
    _run([sys.executable, "-c", code], cwd=scratch, env=cell_env)
    print(f"PACKAGE_MATRIX={name}:PASS")


def main() -> int:
    for path in (CSW_ROOT, DP_ROOT):
        if not path.is_dir():
            raise RuntimeError(f"required sibling source is unavailable: {path}")
    with tempfile.TemporaryDirectory(prefix="cognitive-runtime-package-matrix-") as raw:
        scratch = Path(raw)
        env = dict(os.environ)
        env.update({"PYTHONDONTWRITEBYTECODE": "1",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "PIP_NO_INDEX": "1",
                    "PYTHONPATH": ""})
        core = _build_wheel("runtime-core", DP_ROOT / "cognitive_runtime_core",
                            scratch, env)
        cfw = _build_wheel("cfw", ROOT, scratch, env)
        csw = _build_wheel("csw", CSW_ROOT, scratch, env)
        dp = _build_wheel("dp", DP_ROOT, scratch, env)

        cells = (
            ("runtime-core-only", [core], ("cognitive_runtime_core",),
             ("digital_psychology", "runtime_contract", "runtime_host",
              "cognitive_statework"), False, False, False),
            ("cfw-only", [cfw], ("runtime", "resolve_runtime"),
             ("cognitive_runtime_core", "digital_psychology", "cognitive_statework"),
             True, False, False),
            ("csw-only", [csw], ("cognitive_statework",),
             ("cognitive_runtime_core", "digital_psychology", "runtime_contract"),
             False, True, False),
            ("dp-only-runtime-core", [dp, core],
             ("digital_psychology", "cognitive_runtime_core"),
             ("runtime_contract", "runtime_host", "cognitive_statework"),
             False, False, True),
            ("cfw-runtime-core", [cfw, core],
             ("runtime", "cognitive_runtime_core"),
             ("digital_psychology", "cognitive_statework"), True, False, False),
            ("csw-runtime-core", [csw, core],
             ("cognitive_statework", "cognitive_runtime_core"),
             ("digital_psychology", "runtime_contract", "runtime_host"),
             False, True, False),
            ("cfw-csw-runtime-core", [cfw, csw, core],
             ("runtime", "cognitive_statework", "cognitive_runtime_core"),
             ("digital_psychology",), True, True, False),
            ("full-triad", [cfw, csw, dp, core],
             ("runtime", "cognitive_statework", "digital_psychology",
              "cognitive_runtime_core"), (), True, True, True),
        )
        for (name, wheels, imports, forbidden,
             start_cfw, start_csw, start_dp) in cells:
            _exercise_cell(name, wheels, imports, forbidden=forbidden,
                           scratch=scratch, env=env, start_cfw=start_cfw,
                           start_csw=start_csw, start_dp=start_dp)

        dp_metadata = tomllib.loads((DP_ROOT / "pyproject.toml").read_text(
            encoding="utf-8"))
        laya_pins = dp_metadata.get("project", {}).get(
            "optional-dependencies", {}).get("laya", ())
        if "laya==0.3.22" not in laya_pins:
            raise RuntimeError("the optional Laya runtime pin is missing")
        if importlib.util.find_spec("laya") is None:
            print("PACKAGE_MATRIX=full-triad-laya-extra:BLOCKED_RUNTIME_UNAVAILABLE")
        else:
            _exercise_cell("full-triad-laya-extra", [cfw, csw, dp, core],
                           ("runtime", "cognitive_statework", "digital_psychology",
                            "cognitive_runtime_core", "laya"), forbidden=(),
                           scratch=scratch, env=env,
                           start_cfw=True, start_csw=True, start_dp=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
