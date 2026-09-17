#!/usr/bin/env python3
"""Install CognitiveFrameWorks skills into writable skill registries.

Copies each core SKILL.md plus its references/, examples/, runtime.md, and
adapters/, and copies cogframe/.

Deployment model: shared/ is build-time/source-only. The installed
SKILL.md files are authoring surfaces; the ACTUAL runtime payload is the
compact bundle emitted by resolve-runtime.py (or the runtime.md capsules
injected by scripts/runtime.py). Built-in targets cover common registries,
while --registry NAME=PATH supports any host without making that host part of
the runtime. Never hand-edit installed copies; regenerate with this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
STATEWORK_ROOT = Path(os.environ.get(
    "COGNITIVE_STATEWORK_SOURCE", str(ROOT.parent / "CognitiveStateWork")))
DIGITAL_PSYCHOLOGY_ROOT = Path(os.environ.get(
    "COGNITIVE_DIGITALPSYCHOLOGY_SOURCE", str(ROOT.parent / "DigitalPsychology")))
CORE_SKILLS = ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD", "SISPIS"]

REGISTRIES = {
    "agents": Path.home() / ".agents" / "skills",
    "codex": Path.home() / ".codex" / "skills",
    "harvardcodex": Path.home() / ".harvardcodex" / "skills",
}

MANIFEST_NAME = ".cognitiveframeworks-managed.json"
MANIFEST_VERSION = 1


def _remove_path(path: Path) -> None:
    """Remove only a path explicitly owned by this installer."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _relative(registry: Path, path: Path) -> str:
    return path.resolve().relative_to(registry.resolve()).as_posix()


def _load_owned_paths(registry: Path) -> tuple[set[str], dict[str, str]]:
    manifest = registry / MANIFEST_NAME
    if not manifest.exists():
        return set(), {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        paths = data.get("managed_paths")
        if data.get("schema_version") != MANIFEST_VERSION or not isinstance(paths, list):
            raise ValueError("invalid manifest shape")
        owned = set()
        for value in paths:
            candidate = Path(str(value))
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(f"unsafe managed path {value!r}")
            owned.add(candidate.as_posix())
        digests = data.get("managed_digests", {})
        if not isinstance(digests, dict):
            raise ValueError("invalid managed_digests")
        return owned, {str(key): str(value) for key, value in digests.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"refusing to update registry with invalid ownership manifest {manifest}: {exc}") from exc


def _write_owned_paths(registry: Path, paths: set[str]) -> None:
    manifest = registry / MANIFEST_NAME
    digests = {
        relative: hashlib.sha256((registry / relative).read_bytes()).hexdigest()
        for relative in paths
        if (registry / relative).is_file() and not (registry / relative).is_symlink()
    }
    payload = {"schema_version": MANIFEST_VERSION,
               "installer": "CognitiveFrameWorks",
               "managed_paths": sorted(paths),
               "managed_digests": digests}
    staged = registry / f".{MANIFEST_NAME}.tmp"
    staged.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(staged, manifest)


def _copy_owned_file(source: Path, destination: Path, registry: Path,
                     owned: set[str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    owned.add(_relative(registry, destination))


def _copy_owned_tree(source: Path, destination: Path, registry: Path,
                     owned: set[str]) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            _copy_owned_file(path, destination / path.relative_to(source), registry, owned)


def _cleanup_owned_paths(registry: Path, previous: set[str], current: set[str],
                         previous_digests: dict[str, str]) -> None:
    for relative in sorted(previous - current, key=lambda item: (item.count("/"), item), reverse=True):
        path = registry / relative
        if not (path.exists() or path.is_symlink()):
            continue
        # A managed file may have been edited or replaced by a user after the
        # previous install.  Preserve it unless its content is still exactly
        # the installer-owned version recorded in the manifest.  The runtime
        # root is replaced transactionally and therefore has no per-file
        # cleanup path here.
        expected = previous_digests.get(relative)
        if not expected or path.is_symlink() or not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            _remove_path(path)


def _atomic_replace_directory(staged: Path, destination: Path) -> None:
    """Atomically publish a complete runtime tree, preserving rollback on failure."""
    backup = destination.with_name(f".{destination.name}.previous")
    if backup.exists() or backup.is_symlink():
        _remove_path(backup)
    moved_old = False
    try:
        if destination.exists() or destination.is_symlink():
            os.replace(destination, backup)
            moved_old = True
        os.replace(staged, destination)
    except Exception:
        if destination.exists() or destination.is_symlink():
            _remove_path(destination)
        if moved_old and (backup.exists() or backup.is_symlink()):
            os.replace(backup, destination)
        raise
    finally:
        if backup.exists() or backup.is_symlink():
            _remove_path(backup)


def _registry_specs(targets: list[str] | None, custom: list[str]) -> list[tuple[str, Path]]:
    selected = list(REGISTRIES) if targets and "all" in targets else list(targets or [])
    specs = [(name, REGISTRIES[name]) for name in selected]
    for raw in custom:
        if "=" in raw:
            name, value = raw.split("=", 1)
        else:
            value = raw
            name = Path(value).expanduser().name or "custom"
        path = Path(value).expanduser()
        if not name or not value:
            raise ValueError(f"invalid --registry {raw!r}; expected NAME=PATH")
        specs.append((name, path))
    deduped: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, path in specs:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append((name, path))
    return deduped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", nargs="+", default=None,
                        choices=list(REGISTRIES) + ["all"],
                        help="built-in registries (default: agents unless --registry is used)")
    parser.add_argument("--registry", action="append", default=[], metavar="NAME=PATH",
                        help="arbitrary host registry; repeatable, e.g. claude=$HOME/.claude/skills")
    parser.add_argument("--statework-root", type=Path, default=STATEWORK_ROOT,
                        help="source CognitiveStateWork tree to bundle")
    parser.add_argument("--digital-psychology-root", type=Path,
                        default=DIGITAL_PSYCHOLOGY_ROOT,
                        help="source DigitalPsychology tree to bundle")
    args = parser.parse_args()
    statework_root = args.statework_root.resolve()
    digital_psychology_root = args.digital_psychology_root.resolve()
    missing_sources = [str(path) for path in (statework_root, digital_psychology_root)
                       if not path.is_dir()]
    if missing_sources:
        print("Cannot build a self-contained runtime; missing source tree(s):")
        for path in missing_sources:
            print(f"  - {path}")
        return 1
    selected_targets = args.target if args.target is not None else ([] if args.registry else ["agents"])
    try:
        target_specs = _registry_specs(selected_targets, args.registry)
    except ValueError as exc:
        parser.error(str(exc))

    installed_any = False
    for target, reg in target_specs:
        try:
            # A first install must work for a genuinely empty HOME.  Create
            # only the selected registry inside that HOME; never search for
            # or mutate an unrelated existing registry.
            reg.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[SKIP] {target}: cannot create registry {reg}: {exc}")
            continue
        if not os.access(reg, os.W_OK):
            print(f"[SKIP] {target}: registry {reg} is not writable")
            continue
        print(f"Installing into {reg}")
        previous_owned, previous_digests = _load_owned_paths(reg)
        owned: set[str] = set()
        installed = 0
        for skill in CORE_SKILLS:
            src = ROOT / skill
            if not (src / "SKILL.md").exists():
                print(f"  [SKIP] {skill}: no SKILL.md")
                continue
            dst = reg / skill
            dst.mkdir(parents=True, exist_ok=True)
            _copy_owned_file(src / "SKILL.md", dst / "SKILL.md", reg, owned)
            for sub in ("references", "examples", "adapters"):
                s = src / sub
                if s.is_dir():
                    _copy_owned_tree(s, dst / sub, reg, owned)
            if (src / "runtime.md").exists():
                _copy_owned_file(src / "runtime.md", dst / "runtime.md", reg, owned)
            installed += 1
            print(f"  [ok]   {skill}")

        cf_src = ROOT / "cogframe"
        if (cf_src / "SKILL.md").exists():
            dst = reg / "cogframe"
            dst.mkdir(parents=True, exist_ok=True)
            _copy_owned_file(cf_src / "SKILL.md", dst / "SKILL.md", reg, owned)
            print("  [ok]   cogframe")
            installed += 1

        # Bundle the StateWork wrappers in the same atomic installation.  A
        # runtime that resolves StateWorks but leaves their host-facing
        # capsules in a separate source checkout is not self-contained.
        statework_registry = yaml.safe_load(
            (statework_root / "registry.yaml").read_text(encoding="utf-8"))
        for statework_name in statework_registry.get("stateworks", []):
            source_dir = statework_root / statework_name
            source_skill = source_dir / "SKILL.md"
            if not source_skill.exists():
                source_skill = source_dir / "STATEWORK.md"
            if not source_skill.exists():
                print(f"  [FAIL] {statework_name}: no SKILL.md or STATEWORK.md")
                continue
            destination = reg / statework_name
            destination.mkdir(parents=True, exist_ok=True)
            _copy_owned_file(source_skill, destination / "SKILL.md", reg, owned)
            if (source_dir / "manifest.yaml").exists():
                _copy_owned_file(source_dir / "manifest.yaml", destination / "manifest.yaml", reg, owned)
            capsule = source_dir / "runtime.md"
            fingerprint_source = capsule if capsule.exists() else source_dir / "SKILL.md"
            fingerprint = destination / "fingerprint"
            fingerprint.write_text(hashlib.sha256(fingerprint_source.read_bytes()).hexdigest() + "\n",
                                   encoding="utf-8")
            owned.add(_relative(reg, fingerprint))
            print(f"  [ok]   {statework_name}")
            installed += 1

        print(f"  installed {installed} skill(s) into {reg}")
        runtime_target = reg / "cognitiveframeworks_runtime"
        runtime_dst = Path(tempfile.mkdtemp(prefix=".cognitiveframeworks_runtime.staging-",
                                            dir=reg))
        owned.add(_relative(reg, runtime_target))
        scripts_dst = runtime_dst / "scripts"
        scripts_dst.mkdir(parents=True, exist_ok=True)
        for filename in ("runtime.py", "resolve-runtime.py", "cognitive_runtime.py"):
            shutil.copy2(ROOT / "scripts" / filename, scripts_dst / filename)
        shutil.copytree(ROOT / "scripts" / "runtime_contract",
                        scripts_dst / "runtime_contract")
        shutil.copytree(ROOT / "contracts", runtime_dst / "contracts")
        shutil.copytree(ROOT / "shared", runtime_dst / "shared")
        if statework_root.is_dir():
            bundled_statework = runtime_dst / "statework"
            bundled_statework.mkdir(parents=True, exist_ok=True)
            shutil.copy2(statework_root / "registry.yaml", bundled_statework / "registry.yaml")
            shutil.copytree(statework_root / "schemas", bundled_statework / "schemas")
            bundled_scripts = bundled_statework / "scripts"
            bundled_scripts.mkdir(parents=True, exist_ok=True)
            for filename in ("__init__.py", "control_plane.py",
                             "validate-repository-truth.py"):
                source_script = statework_root / "scripts" / filename
                if source_script.exists():
                    shutil.copy2(source_script, bundled_scripts / filename)
            for statework_name in (yaml.safe_load(
                    (statework_root / "registry.yaml").read_text(encoding="utf-8")
            ).get("stateworks", [])):
                shutil.copytree(statework_root / statework_name,
                                bundled_statework / statework_name)
        if digital_psychology_root.is_dir():
            shutil.copytree(digital_psychology_root / "schemas",
                            runtime_dst / "digitalpsychology" / "schemas")
        (runtime_dst / "plugin.json").write_text(
            (ROOT / "plugin.json").read_text(encoding="utf-8"), encoding="utf-8")
        for skill in CORE_SKILLS:
            source_capsule = ROOT / skill / "runtime.md"
            if source_capsule.exists():
                destination = runtime_dst / skill
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_capsule, destination / "runtime.md")
        sispis_runtime = ROOT / "SISPIS" / "runtime"
        if sispis_runtime.is_dir():
            shutil.copytree(sispis_runtime, runtime_dst / "SISPIS" / "runtime")
        calibration = ROOT / "SISPIS" / "references" / "signal-calibration.yaml"
        if calibration.exists():
            destination = runtime_dst / "SISPIS" / "references"
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(calibration, destination / calibration.name)
        (runtime_dst / "host-interceptor.json").write_text(
            json.dumps({"entrypoint": "cognitive_runtime:CognitiveRuntime",
                        "api": ["begin_task", "before_model_call", "before_tool_planning",
                                "before_tool", "before_retry", "after_tool",
                                "observation", "after_validator",
                                "operator_observation", "request_transition",
                                "request_completion", "validate_completion_boundary",
                                "publish_handoff", "consume_handoff", "advance_segment",
                                "resolve_contradiction", "finish_task"],
                        "enforcement_owner": "CognitiveFrameWorks",
                        "observation_consumer": "DigitalPsychology"}, indent=2) + "\n",
            encoding="utf-8")
        _atomic_replace_directory(runtime_dst, runtime_target)
        _cleanup_owned_paths(reg, previous_owned, owned, previous_digests)
        _write_owned_paths(reg, owned)
        print(f"  [ok]   host interceptor registered at {runtime_target}")
        installed_any = True
        installed_any = installed_any or installed > 0

    print("\nRun scripts/doctor.py to verify routability.")
    return 0 if installed_any else 1


if __name__ == "__main__":
    sys.exit(main())
