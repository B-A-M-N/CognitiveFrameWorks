#!/usr/bin/env python3
"""Install CognitiveFrameWorks skills into writable skill registries.

Copies each core SKILL.md plus its references/, examples/, runtime.md, and
adapters/, and copies cogframe/.

Deployment model: shared/ is build-time/source-only. The installed
SKILL.md files are authoring surfaces; the ACTUAL runtime payload is the
compact bundle emitted by resolve-runtime.py (or the runtime.md capsules
injected by scripts/runtime.py). Explicit --target selects registries:
agents | codex | harvardcodex | all. Never hand-edit installed copies;
regenerate with this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", nargs="+", default=["harvardcodex"],
                        choices=list(REGISTRIES) + ["all"],
                        help="registries to install into")
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
    targets = list(REGISTRIES) if "all" in args.target else args.target

    installed_any = False
    for target in targets:
        reg = REGISTRIES[target]
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
        installed = 0
        for skill in CORE_SKILLS:
            src = ROOT / skill
            if not (src / "SKILL.md").exists():
                print(f"  [SKIP] {skill}: no SKILL.md")
                continue
            dst = reg / skill
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / "SKILL.md", dst / "SKILL.md")
            for sub in ("references", "examples", "adapters"):
                s = src / sub
                if s.is_dir():
                    d = dst / sub
                    if d.exists():
                        shutil.rmtree(d)
                    shutil.copytree(s, d)
            if (src / "runtime.md").exists():
                shutil.copy2(src / "runtime.md", dst / "runtime.md")
            installed += 1
            print(f"  [ok]   {skill}")

        cf_src = ROOT / "cogframe"
        if (cf_src / "SKILL.md").exists():
            dst = reg / "cogframe"
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cf_src / "SKILL.md", dst / "SKILL.md")
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
            shutil.copy2(source_skill, destination / "SKILL.md")
            if (source_dir / "manifest.yaml").exists():
                shutil.copy2(source_dir / "manifest.yaml", destination / "manifest.yaml")
            capsule = source_dir / "runtime.md"
            fingerprint_source = capsule if capsule.exists() else source_dir / "SKILL.md"
            (destination / "fingerprint").write_text(
                hashlib.sha256(fingerprint_source.read_bytes()).hexdigest() + "\n",
                encoding="utf-8")
            print(f"  [ok]   {statework_name}")
            installed += 1

        print(f"  installed {installed} skill(s) into {reg}")
        runtime_dst = reg / "cognitiveframeworks_runtime"
        if runtime_dst.exists():
            shutil.rmtree(runtime_dst)
        runtime_dst.mkdir(parents=True, exist_ok=True)
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
        print(f"  [ok]   host interceptor registered at {runtime_dst}")
        installed_any = True
        installed_any = installed_any or installed > 0

    print("\nRun scripts/doctor.py to verify routability.")
    return 0 if installed_any else 1


if __name__ == "__main__":
    sys.exit(main())
