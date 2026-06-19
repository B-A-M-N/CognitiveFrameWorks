#!/usr/bin/env python3
"""Validate CognitiveFrameWorks structure and generated artifacts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EXPECTED_SKILLS = ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD", "SISPIS"]
IGNORED_DIRS = {".git", ".crush", ".claude", "__pycache__"}
ADAPTER_SUFFIXES = {".md", ".yml", ".yaml"}


def iter_files(root: Path = ROOT) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & IGNORED_DIRS:
            continue
        yield path


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def fail(messages: List[str]) -> None:
    if messages:
        print("Validation failed:")
        for msg in messages:
            print(f"- {msg}")
        sys.exit(1)
    print("Validation passed")


def validate_json_files(messages: List[str]) -> None:
    for path in iter_files():
        if path.suffix.lower() != ".json":
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            messages.append(f"Invalid JSON in {rel(path)}: {exc}")


def validate_skill_count(messages: List[str]) -> None:
    skill_files = sorted(ROOT.glob("*/SKILL.md"))
    found = sorted(p.parent.name for p in skill_files)
    expected = sorted(EXPECTED_SKILLS)
    if found != expected:
        messages.append(f"Skill count drift: expected {expected}, found {found}")


def validate_integration_doc(messages: List[str]) -> None:
    path = ROOT / "shared" / "integration.md"
    if not path.exists():
        messages.append("Missing shared/integration.md")
        return
    text = path.read_text(encoding="utf-8")
    if "intent_weight" not in text:
        messages.append("shared/integration.md does not define intent_weight mapping")
    if "DOX is not a SISPIS upstream source" not in text:
        messages.append("shared/integration.md does not exclude DOX from SISPIS upstream signals")


def validate_adapter_source(messages: List[str]) -> None:
    path = ROOT / "shared" / "adapter-source.md"
    if not path.exists():
        messages.append("Missing shared/adapter-source.md")
        return
    text = path.read_text(encoding="utf-8")
    if "Generated adapter files" not in text:
        messages.append("shared/adapter-source.md does not describe generated adapter files")


def validate_claude_contract(messages: List[str]) -> None:
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    if "load these seven skills" in text:
        messages.append("CLAUDE.md still instructs loading all seven skills by default")
    if "Upstream skills (OWL, FUSE, WARD, FLOW, ANCHOR, DOX)" in text:
        messages.append("CLAUDE.md still lists DOX as a SISPIS upstream skill")
    if "shared/integration.md" not in text:
        messages.append("CLAUDE.md does not reference shared/integration.md")


def validate_ward_recover(messages: List[str]) -> None:
    text = (ROOT / "WARD" / "references" / "authority-boundaries.md").read_text(encoding="utf-8")
    if "state_recovery_required" not in text:
        messages.append("WARD missing state_recovery_required signal for recover required_action")
    if "`recover`" not in text or "recover" not in text:
        messages.append("WARD required_action taxonomy does not expose recover")


def validate_adapter_generation(messages: List[str]) -> None:
    generator_path = ROOT / "scripts" / "generate-adapters.py"
    try:
        spec = importlib.util.spec_from_file_location("generate_adapters", generator_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {rel(generator_path)}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        build_adapter_outputs = module.build_adapter_outputs
    except Exception as exc:
        messages.append(f"Cannot import adapter generator: {exc}")
        return

    expected = build_adapter_outputs()
    for path, content in sorted(expected.items()):
        if not path.exists():
            messages.append(f"Missing generated adapter {rel(path)}")
            continue
        if path.read_text(encoding="utf-8") != content:
            messages.append(f"Generated adapter drift: {rel(path)}")

    for path in iter_files():
        if path.parent.name != "adapters":
            continue
        if path not in expected:
            messages.append(f"Unexpected adapter file: {rel(path)}")


def validate_adapter_duplicate_hashes(messages: List[str]) -> None:
    hashes: Dict[str, List[Path]] = {}
    for path in iter_files():
        if path.parent.name != "adapters":
            continue
        data = path.read_bytes()
        hashes.setdefault(hashlib.sha256(data).hexdigest(), []).append(path)

    expected_pairs = {
        "CLAUDE.md",
        ".cursorrules",
        ".windsurfrules",
        "system-prompt.md",
        ".aider.conf.yml",
        "continue-config.yaml",
    }
    for digest, paths in sorted(hashes.items()):
        if len(paths) < 2:
            continue
        names = {p.name for p in paths}
        if not names <= expected_pairs:
            messages.append(f"Unexpected duplicate adapter hash group: {[rel(p) for p in paths]}")


def main() -> None:
    messages: List[str] = []
    validate_json_files(messages)
    validate_skill_count(messages)
    validate_integration_doc(messages)
    validate_adapter_source(messages)
    validate_claude_contract(messages)
    validate_ward_recover(messages)
    validate_adapter_generation(messages)
    validate_adapter_duplicate_hashes(messages)
    fail(messages)


if __name__ == "__main__":
    main()
