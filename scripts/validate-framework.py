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
ADAPTER_VARIANTS = ["CLAUDE.md", ".cursorrules", ".windsurfrules", "system-prompt.md", ".aider.conf.yml", "continue-config.yaml"]
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
    skill_files = sorted(p for p in ROOT.glob("*/SKILL.md") if p.parent.name in EXPECTED_SKILLS)
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


CANONICAL_CONTRACTS = {
    "OWL": {
        "signal-schema.md": [
            "`user_observation_conflict` | 2.0",
            "`circular_verification` | 2.0",
            "`cohesion_risk` | 2.0",
        ],
        "pressure-protocol.md": [
            "## Sycophancy Detection",
            "User challenges prior conclusion",
            "reports observed behavior/failure",
            "Preference / intent disagreement",
            "The agent's previous conclusion is not privileged evidence",
            "user_observation_conflict",
        ],
    },
    "FUSE": {
        "execution-strategy.md": [
            "`conflicting_evidence_unrechecked` | 1.0",
            "`self_validating_evidence` | 1.0",
            "| User reports feature still failing |",
            "| Agent-authored requirement-level regression test passes |",
            "| Agent-authored structural test mirrors implementation |",
            "stale evidence cannot satisfy Necessity",
        ],
    },
    "ANCHOR": {
        "execution-continuity.md": [
            "Resolved ──[success criterion contradicted by new observation]──→ Active",
            "Partially Resolved ──[remaining behavior observed]─────────────→ Active",
            "A reopen is a reopen",
            "A user-reported observation is evidence",
            "Unknown unless available evidence supports an inference",
        ],
    },
    "FLOW": {
        "operational-efficiency.md": [
            "`responsibility_concentration` | 2.0",
            "`change_amplification` | 1.0",
            "Responsibility concentration / central-object growth",
            "thin coordinator plus cohesive collaborators",
        ],
    },
}


def validate_behavior_contracts(messages: List[str]) -> None:
    for skill, references in CANONICAL_CONTRACTS.items():
        for filename, required_phrases in references.items():
            path = ROOT / skill / "references" / filename
            if not path.exists():
                messages.append(f"Missing behavior contract: {rel(path)}")
                continue
            text = path.read_text(encoding="utf-8")
            for phrase in required_phrases:
                if phrase not in text:
                    messages.append(f"{rel(path)} missing canonical contract phrase: {phrase!r}")

    integration = ROOT / "shared" / "integration.md"
    if integration.exists():
        text = integration.read_text(encoding="utf-8")
        for phrase in [
            "## User Contradiction Path",
            "## Structural Concentration Path",
            "user_observation_conflict",
            "fresh evidence required; stale evidence cannot satisfy Necessity",
            "cohesion_risk",
            "responsibility_concentration/change_amplification",
            "cannot classify responsibility extraction as premature solely because there is one caller",
        ]:
            if phrase not in text:
                messages.append(f"shared/integration.md missing cross-skill contract phrase: {phrase!r}")


def validate_scenario_manifest(messages: List[str]) -> None:
    registries = {
        "OWL": {"user_observation_conflict": 2.0, "circular_verification": 2.0, "cohesion_risk": 2.0, "position_pressure": 1.0, "scope_expansion": 1.0},
        "FUSE": {"conflicting_evidence_unrechecked": 1.0, "self_validating_evidence": 1.0},
        "FLOW": {"responsibility_concentration": 2.0, "change_amplification": 1.0},
    }
    path = ROOT / "shared" / "scenarios.json"
    if not path.exists():
        messages.append("Missing shared/scenarios.json")
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        messages.append(f"Invalid scenario manifest {rel(path)}: {exc}")
        return
    scenarios = manifest.get("scenarios") if isinstance(manifest, dict) else None
    if not isinstance(scenarios, list) or not scenarios:
        messages.append("Scenario manifest has no scenarios")
        return

    seen_ids = set()
    expected_classes = {
        "user_observation_conformance",
        "circular_verification",
        "responsibility_concentration",
    }
    found_classes = set()
    for index, scenario in enumerate(scenarios):
        where = f"scenario[{index}]"
        if not isinstance(scenario, dict):
            messages.append(f"{where} is not a mapping")
            continue
        scenario_id = scenario.get("id")
        if not scenario_id or scenario_id in seen_ids:
            messages.append(f"{where} has missing or duplicate id: {scenario_id!r}")
        seen_ids.add(scenario_id)
        scenario_class = scenario.get("class")
        found_classes.add(scenario_class)
        if scenario_class not in expected_classes:
            messages.append(f"{where} has unknown class: {scenario_class!r}")
        if not scenario.get("input"):
            messages.append(f"{where} has no input")
        required_actions = scenario.get("required_actions")
        if not isinstance(required_actions, list) or not required_actions:
            messages.append(f"{where} has no expected next action")
        forbidden_actions = scenario.get("forbidden_actions")
        if not isinstance(forbidden_actions, list) or not forbidden_actions:
            messages.append(f"{where} has no forbidden behavior")
        signals = scenario.get("expected_signals", [])
        if not isinstance(signals, list):
            messages.append(f"{where} expected_signals is not a list")
            continue
        for signal_index, signal in enumerate(signals):
            signal_where = f"{where}.expected_signals[{signal_index}]"
            if not isinstance(signal, dict):
                messages.append(f"{signal_where} is not a mapping")
                continue
            skill = signal.get("skill")
            signal_type = signal.get("signal_type")
            weight = signal.get("weight")
            registry = registries.get(skill, {})
            if signal_type not in registry:
                messages.append(f"{signal_where} references unknown signal {skill}.{signal_type}")
            elif weight != registry[signal_type]:
                messages.append(f"{signal_where} weight for {skill}.{signal_type}: expected {registry[signal_type]}, found {weight}")

    missing_classes = expected_classes - found_classes
    if missing_classes:
        messages.append(f"Scenario manifest missing expected classes: {sorted(missing_classes)}")


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
        MARKDOWN_HEADER = module.MARKDOWN_HEADER
        YAML_HEADER = module.YAML_HEADER
    except Exception as exc:
        messages.append(f"Cannot import adapter generator: {exc}")
        return

    # Check every skill has all 6 expected adapter variants
    for skill in EXPECTED_SKILLS:
        canonical_path = ROOT / skill / "adapters" / "CLAUDE.md"
        if not canonical_path.exists():
            continue
        adapter_dir = canonical_path.parent
        for variant in ADAPTER_VARIANTS:
            target = adapter_dir / variant
            if not target.exists():
                messages.append(f"Missing adapter file: {rel(target)}")

    # Check content matches generated output
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
    validate_behavior_contracts(messages)
    validate_scenario_manifest(messages)
    validate_claude_contract(messages)
    validate_ward_recover(messages)
    validate_adapter_generation(messages)
    validate_adapter_duplicate_hashes(messages)
    fail(messages)


if __name__ == "__main__":
    main()
