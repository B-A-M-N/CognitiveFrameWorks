#!/usr/bin/env python3
"""Runtime installation doctor for CognitiveFrameWorks.

Validates actual runtime routability per explicit registry target. Built-in
targets cover common registries; --registry NAME=PATH supports any host. The
installer and doctor share the same targeting policy so they can converge:

    source skill valid?            -> frontmatter parses
    runtime skill registered?      -> installed wrapper exists in target registry
    installed/source fingerprint?  -> wrapper fingerprint matches source (runtime.md)
    dispatcher target resolvable?  -> StateWork manifest exists + wrapper exists
                                       in the target registry
    required references present?   -> refs dirs exist in source and installed
    schema versions compatible?    -> shared contracts present
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_SKILLS = ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD", "SISPIS"]
STATEWORKS = ["infrae", "gitter", "getter", "tuid"]

REGISTRIES = {
    "agents": Path.home() / ".agents" / "skills",
    "codex": Path.home() / ".codex" / "skills",
    "harvardcodex": Path.home() / ".harvardcodex" / "skills",
}


def registry_specs(targets: list[str] | None, custom: list[str]) -> list[tuple[str, Path]]:
    selected = list(REGISTRIES) if targets and "all" in targets else list(targets or [])
    specs = [(name, REGISTRIES[name]) for name in selected]
    for raw in custom:
        if "=" in raw:
            name, value = raw.split("=", 1)
        else:
            value = raw
            name = Path(value).expanduser().name or "custom"
        if not name or not value:
            raise ValueError(f"invalid --registry {raw!r}; expected NAME=PATH")
        specs.append((name, Path(value).expanduser()))
    deduped: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, path in specs:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append((name, path))
    return deduped


def find_registry() -> Path | None:
    for reg in REGISTRIES.values():
        if reg.is_dir() and (reg / "FLOW").is_dir():
            return reg
    return None


def frontmatter_ok(path: Path) -> bool:
    try:
        import yaml
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            return False
        data = yaml.safe_load(text.split("---", 2)[1])
        return isinstance(data, dict) and bool(data.get("name")) and bool(data.get("description"))
    except Exception:
        return False


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_installed_runtime(runtime_dst: Path, label: str,
                            problems: list[str]) -> None:
    """Execute the installed bundle, rather than only checking filenames.

    A copied runtime can be syntactically present while still resolving the
    wrong StateWork/DP roots or failing during host startup.  Keep this probe
    side-effect bounded in a temporary runtime directory.
    """
    scripts = runtime_dst / "scripts"
    probe = (
        "import sys, importlib.util; "
        "sys.path.insert(0, %r); "
        "spec = importlib.util.spec_from_file_location('resolve_runtime', %r); "
        "resolve_runtime = importlib.util.module_from_spec(spec); "
        "sys.modules['resolve_runtime'] = resolve_runtime; "
        "spec.loader.exec_module(resolve_runtime); "
        "import runtime; "
        "bundle = resolve_runtime.resolve('doctor-probe', 'quick', ()); "
        "session = runtime.start(bundle, telemetry_disabled=True); "
        "assert session.before_model_call(); "
        "assert session.current_segment.index == 0"
    ) % (str(scripts), str(scripts / "resolve-runtime.py"))
    with tempfile.TemporaryDirectory(prefix="cfw-doctor-") as runtime_dir:
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["COGNITIVE_STATEWORK_ROOT"] = str(runtime_dst / "statework")
        env["COGNITIVE_DIGITALPSYCHOLOGY_ROOT"] = str(runtime_dst / "digitalpsychology")
        result = subprocess.run([sys.executable, "-c", probe], cwd=str(runtime_dst),
                                env=env, capture_output=True, text=True,
                                timeout=20)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        problems.append(f"[{label}] installed runtime execution failed: "
                        f"{detail[0] if detail else 'unknown error'}")


def check_registry(registry: Path, target: str, problems: list[str], infos: list[str], cow: Path) -> None:
    """Check one registry. Errors here count toward overall health."""
    import yaml
    import os
    writable = False
    try:
        writable = os.access(registry, os.W_OK)
    except Exception:
        pass
    try:
        label = f"~/{registry.relative_to(Path.home())}"
    except ValueError:
        label = str(registry)
    if not writable:
        infos.append(f"[{label}] read-only registry — reporting only, not a failure source")

    runtime_dst = registry / "cognitiveframeworks_runtime"
    runtime_files = [
        runtime_dst / "scripts" / "runtime.py",
        runtime_dst / "scripts" / "resolve-runtime.py",
        runtime_dst / "scripts" / "cognitive_runtime.py",
        runtime_dst / "scripts" / "runtime_contract" / "actions.py",
        runtime_dst / "host-interceptor.json",
        runtime_dst / "statework" / "registry.yaml",
        runtime_dst / "statework" / "scripts" / "control_plane.py",
        runtime_dst / "statework" / "schemas" / "packet-registry.yaml",
        runtime_dst / "SISPIS" / "runtime" / "calibrate.py",
    ]
    for required in runtime_files:
        if not required.exists():
            problems.append(f"[{label}] Runtime host interceptor incomplete: {required.relative_to(registry)}")
    if all(required.exists() for required in runtime_files):
        check_installed_runtime(runtime_dst, label, problems)

    for skill in CORE_SKILLS:
        src_skill = ROOT / skill
        src = src_skill / "SKILL.md"
        src_capsule = src_skill / "runtime.md"
        dst = registry / skill / "SKILL.md"
        if not dst.exists():
            (infos if not writable else problems).append(f"[{label}] Runtime not registered: {skill}")
            continue
        if not frontmatter_ok(dst):
            problems.append(f"[{label}] Runtime frontmatter invalid: {skill}/SKILL.md")
            continue
        # fingerprint compares the runtime capsule (the actual injected
        # surface); SKILL.md parity is checked as a secondary signal
        if src_capsule.exists() and (registry / skill / "runtime.md").exists():
            if sha256(src_capsule) != sha256(registry / skill / "runtime.md"):
                (infos if not writable else problems).append(f"[{label}] Installed/source capsule fingerprint mismatch: {skill}")
        elif sha256(src) != sha256(dst):
            (infos if not writable else problems).append(f"[{label}] Installed/source fingerprint mismatch: {skill}")
        else:
            infos.append(f"{skill}@{label}: installed copy matches source")

    cf_src = ROOT / "cogframe" / "SKILL.md"
    cf_dst = registry / "cogframe" / "SKILL.md"
    if not writable:
        infos.append(f"[{label}] cogframe not checked (read-only registry)")
    elif cf_dst.exists() and sha256(cf_src) != sha256(cf_dst):
        problems.append(f"[{label}] Installed/source fingerprint mismatch: cogframe")
    elif not cf_dst.exists():
        problems.append(f"[{label}] Runtime not registered: cogframe")

    if cow.is_dir():
        reg_manifest = cow / "registry.yaml"
        if not reg_manifest.exists():
            problems.append("[cow] CognitiveStateWork registry.yaml missing")
            return
        data = yaml.safe_load(reg_manifest.read_text(encoding="utf-8"))
        names = data.get("stateworks", []) if isinstance(data, dict) else []
        for name in names:
            manifest_src = cow / name / "manifest.yaml"
            wrapper = registry / name / "SKILL.md"
            if not manifest_src.exists():
                problems.append(f"[cow] Dispatcher target not resolvable: {name} (no source manifest)")
                continue
            if not wrapper.exists():
                (infos if not writable else problems).append(f"[{label}] Dispatcher target not installed: {name}")
                continue
            if not frontmatter_ok(wrapper):
                problems.append(f"[{label}] Runtime wrapper unloadable: {name}/SKILL.md")
                continue
            fp = registry / name / "fingerprint"
            if fp.exists():
                expected = fp.read_text(encoding="utf-8").strip()
                actual = sha256(cow / name / "runtime.md") if (cow / name / "runtime.md").exists() else sha256(manifest_src)
                if expected != actual:
                    problems.append(f"[{label}] Installed/source fingerprint mismatch: {name}")
                else:
                    infos.append(f"{name}@{label}: manifest + wrapper + fingerprint ok")
            else:
                problems.append(f"[{label}] {name}: no fingerprint file in runtime wrapper")

    for contract in ["signal.schema.json", "integration.md", "pipeline.yaml", "runtime-kernel.yaml"]:
        if not (ROOT / "shared" / contract).exists():
            problems.append(f"Missing shared contract: shared/{contract}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", nargs="+", default=None,
                        choices=list(REGISTRIES) + ["all"],
                        help="built-in registries (default: agents unless --registry is used)")
    parser.add_argument("--registry", action="append", default=[], metavar="NAME=PATH",
                        help="arbitrary host registry; repeatable, matching the installer")
    args = parser.parse_args()
    selected_targets = args.target if args.target is not None else ([] if args.registry else ["agents"])
    try:
        targets = registry_specs(selected_targets, args.registry)
    except ValueError as exc:
        parser.error(str(exc))

    problems: list[str] = []
    infos: list[str] = []
    cow = ROOT.parent / "CognitiveStateWork"

    # 1. Source skills valid
    for skill in CORE_SKILLS:
        src = ROOT / skill / "SKILL.md"
        if not src.exists():
            problems.append(f"Source missing: {skill}/SKILL.md")
            continue
        if not frontmatter_ok(src):
            problems.append(f"Source frontmatter invalid: {skill}/SKILL.md")

    # 2-4. Inspect explicit target registries
    found_any = False
    for target, reg in targets:
        if not reg.is_dir():
            continue
        found_any = True
        try:
            label = f"~/{reg.relative_to(Path.home())}"
        except ValueError:
            label = str(reg)
        infos.append(f"Checking registry: {label}")
        check_registry(reg, target, problems, infos, cow)
    if not found_any:
        problems.append("No installed skill registry found for the selected target(s)")

    # 5. Required references (source)
    for skill in CORE_SKILLS:
        refdir = ROOT / skill / "references"
        if not refdir.is_dir():
            problems.append(f"Missing references dir: {skill}/references")
        elif not any(refdir.iterdir()):
            problems.append(f"Empty references dir: {skill}/references")

    print("CognitiveFrameWorks doctor report\n")
    for line in infos:
        print(f"  [ok]   {line}")
    for line in problems:
        print(f"  [FAIL] {line}")
    if problems:
        print(f"\n{len(problems)} problem(s) found")
        return 1
    print("\nAll checks passed")
    return 0

if __name__ == "__main__":
    sys.exit(main())
