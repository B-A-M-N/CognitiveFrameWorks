#!/usr/bin/env python3
"""Generate adapter variants from each skill's canonical adapters/CLAUDE.md."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD", "SISPIS"]
MARKDOWN_HEADER = "<!-- Generated from shared/adapter-source.md. Do not edit directly. -->\n\n"
YAML_HEADER = "# Generated from shared/adapter-source.md. Do not edit directly.\n"


def strip_generated_header(content: str) -> str:
    lines = content.splitlines()
    if lines and lines[0].strip() == "<!-- Generated from shared/adapter-source.md. Do not edit directly. -->":
        if len(lines) > 1 and lines[1].strip() == "":
            lines = lines[2:]
        else:
            lines = lines[1:]
    return "\n".join(lines).rstrip() + "\n"


def indent_markdown(content: str, spaces: int = 2) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else "" for line in content.splitlines()) + "\n"


def build_adapter_outputs() -> Dict[Path, str]:
    outputs: Dict[Path, str] = {}

    for skill in SKILLS:
        skill_dir = ROOT / skill
        canonical_path = skill_dir / "adapters" / "CLAUDE.md"
        if not canonical_path.exists():
            continue

        canonical = strip_generated_header(canonical_path.read_text(encoding="utf-8"))
        adapter_dir = skill_dir / "adapters"

        for name in ["CLAUDE.md", ".cursorrules", ".windsurfrules"]:
            target = adapter_dir / name
            if target.exists():
                outputs[target] = MARKDOWN_HEADER + canonical

        system_prompt = adapter_dir / "system-prompt.md"
        if system_prompt.exists():
            outputs[system_prompt] = MARKDOWN_HEADER + "# System Prompt\n\n" + canonical

        aider = adapter_dir / ".aider.conf.yml"
        if aider.exists():
            outputs[aider] = YAML_HEADER + "system-prompt: |\n" + indent_markdown(canonical.rstrip(), 2)

        continue_config = adapter_dir / "continue-config.yaml"
        if continue_config.exists():
            outputs[continue_config] = YAML_HEADER + "systemMessage: |\n" + indent_markdown(canonical.rstrip(), 2)

    return outputs


def generate() -> None:
    for path, content in sorted(build_adapter_outputs().items()):
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    generate()
