#!/usr/bin/env python3
"""Validate CognitiveFrameWorks structure and generated artifacts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
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


def validate_frontmatter_loadability(messages: List[str]) -> None:
    """Every skill manifest (each */SKILL.md and cogframe/SKILL.md) must parse
    as YAML frontmatter. A framework validator that does not validate actual
    loadability gives false assurance (FLOW regression)."""
    import yaml

    for path in sorted(ROOT.glob("*/SKILL.md")):
        frontmatter = _read_frontmatter(path)
        if frontmatter is None:
            messages.append(f"{rel(path)} has no YAML frontmatter")
            continue
        try:
            data = yaml.safe_load(frontmatter)
        except Exception as exc:
            messages.append(f"{rel(path)} frontmatter is not valid YAML: {exc}")
            continue
        if not isinstance(data, dict):
            messages.append(f"{rel(path)} frontmatter is not a mapping")
            continue
        if not data.get("name"):
            messages.append(f"{rel(path)} frontmatter has no name")
        if not data.get("description"):
            messages.append(f"{rel(path)} frontmatter has no description")


def _read_frontmatter(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1]


def _validate_producer_consumer_in_dag(messages: List[str], registry: dict) -> None:
    """Every registry signal's producer and every consumer must be a stage in
    the pipeline DAG; a signal whose producer and consumer disagree with the
    manifest's data-flow is a contract violation (reverse check)."""
    pipeline = ROOT / "shared" / "pipeline.yaml"
    if not pipeline.exists():
        return
    import yaml
    try:
        data = yaml.safe_load(pipeline.read_text(encoding="utf-8"))
    except Exception:
        return
    stages = data.get("stages", []) if isinstance(data, dict) else []
    stage_ids = {s.get("id") for s in stages if isinstance(s, dict)}
    # aliases map owner names (e.g. anchor) to concrete stages
    aliases = data.get("aliases", {}) or {}
    # map stage -> signal outputs (input/output fields describe typed signals)
    stage_outputs: Dict[str, List[str]] = {}
    for s in stages:
        sid = s.get("id")
        outputs = s.get("output", [])
        stage_outputs[sid] = [str(o) for o in outputs]
    # producers are owners; a producer is resolvable if any alias member or
    # any stage with that owner declares the signal as an output.
    def owning_stages(owner):
        return [s.get("id") for s in stages if s.get("owner") == owner] + list(aliases.get(owner, []))
    for sig in registry.get("signals", []):
        producer = sig.get("producer")
        owners = owning_stages(producer)
        if not owners:
            messages.append(f"signal {sig.get('id')} producer {producer!r} not in pipeline DAG")
            continue
        declared = False
        for sid in owners:
            emitted = stage_outputs.get(sid, [])
            if any(sig.get("id") in o or sig.get("id").split(".")[-1] in o for o in emitted):
                declared = True
        if not declared:
            messages.append(f"signal {sig.get('id')} producer {producer} does not declare it in pipeline output")


def validate_signal_contracts(messages: List[str]) -> None:
    """Shared/signals.md, shared/signal-registry.json, and
    shared/signal.schema.json must agree; the stale variant must be gone;
    no private SISPIS delta tables in upstream skills."""
    import jsonschema

    reg_path = ROOT / "shared" / "signal-registry.json"
    schema_path = ROOT / "shared" / "signal.schema.json"
    signals_path = ROOT / "shared" / "signals.md"
    registry = json.loads(reg_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    signals = signals_path.read_text(encoding="utf-8")
    _validate_producer_consumer_in_dag(messages, registry)
    reg_ids = {s["id"] for s in registry["signals"]}
    # The deprecated spelling may appear only as a pointer (e.g. "legacy
    # spelling evidence_overclaim is not valid"), never as a registry row or
    # a first-class table entry. Reject it only in that first-class position.
    if re.search(r"^\| `evidence_overclaim` ", signals, re.MULTILINE):
        messages.append("shared/signals.md still registers deprecated evidence_overclaim as a signal")
    for sid in reg_ids:
        if not sid.startswith(("owl.", "anchor.", "fuse.", "flow.", "ward.")):
            messages.append(f"signal {sid} is not namespaced")
        if f"`{sid}`" not in signals:
            messages.append(f"shared/signals.md missing canonical id {sid}")
    # schema source enum and pattern must match registry producers
    for s in registry["signals"]:
        producer = s["producer"]
        if producer not in schema["properties"]["source"]["enum"]:
            messages.append(f"signal {s['id']} producer {producer} not in schema enum")
        sig_prefix = s["id"].split(".")[0]
        if sig_prefix != producer:
            messages.append(f"signal {s['id']} namespace {sig_prefix!r} does not match producer {producer!r}")
        # every registry consumer must be a real stage owner in the pipeline
        stage_ids = {st.get("id") for st in _load_pipeline_stages()}
        owners = {st.get("owner") for st in _load_pipeline_stages()}
        for consumer in s.get("consumers", []):
            if consumer not in owners and consumer not in stage_ids:
                messages.append(f"signal {s['id']} consumer {consumer!r} not in pipeline DAG")
    # registry consumers are machine-owned (not just in the Markdown table)
    for s in registry["signals"]:
        if "consumers" not in s or not isinstance(s.get("consumers"), list):
            messages.append(f"signal {s['id']} has no machine-readable consumers list")
    # envelope example validates against schema
    example = {
        "schema_version": "1.1.0",
        "signal_id": "sig-123",
        "cause_id": "cause-42",
        "source": "fuse",
        "signal_type": "fuse.overclaimed_evidence",
        "severity": "medium",
        "scope": "artifact",
        "evidence_refs": ["test-31", "claim-9"],
        "required_action": "reclassify",
    }
    try:
        jsonschema.validate(instance=example, schema=schema)
    except Exception as exc:
        messages.append(f"canonical envelope example fails schema: {exc}")
    # shared/integration.md must carry the envelope and no upstream SISPIS math
    integration = (ROOT / "shared" / "integration.md").read_text(encoding="utf-8")
    for field in ["signal_id", "cause_id", "source", "signal_type", "severity", "scope", "evidence_refs", "required_action"]:
        if field not in integration:
            messages.append(f"shared/integration.md missing envelope field {field}")
    if "intent_weight" in integration or "entropy_delta" in integration:
        messages.append("shared/integration.md still contains upstream SISPIS math (intent_weight/entropy_delta)")


def _load_pipeline_stages() -> list:
    pipeline = ROOT / "shared" / "pipeline.yaml"
    if not pipeline.exists():
        return []
    import yaml
    try:
        data = yaml.safe_load(pipeline.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data.get("stages", []) if isinstance(data, dict) else []


def validate_no_upstream_sispis_math(messages: List[str]) -> None:
    """OWL/FUSE/FLOW/WARD/ANCHOR skills and references must not compute SISPIS
    entropy/intent deltas; SISPIS alone owns signal → calibration."""
    offenders = []
    for skill in ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD"]:
        for path in sorted((ROOT / skill).rglob("*")):
            if not path.is_file() or not path.suffix.lower() in {".md", ".yaml", ".yml"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "intent_weight" in text or "entropy_delta" in text or ("SISPIS signal" in text and "Delta" in text):
                offenders.append(rel(path))
    if offenders:
        messages.append("Upstream SISPIS math still present in: " + ", ".join(offenders))


def validate_pipeline_manifest(messages: List[str]) -> None:
    """shared/pipeline.yaml and shared/signal.schema.json exist; pipeline
    stages carry requires/order_after/execution_mode/frequency semantics;
    profiles resolve through aliases; hard-dependency closure holds for every
    activation profile; order_after forms a DAG with sispis terminal."""
    import collections
    import yaml

    schema_path = ROOT / "shared" / "signal.schema.json"
    if not schema_path.exists():
        messages.append("Missing shared/signal.schema.json")
    pipeline = ROOT / "shared" / "pipeline.yaml"
    if not pipeline.exists():
        messages.append("Missing shared/pipeline.yaml (canonical pipeline manifest)")
        return
    try:
        data = yaml.safe_load(pipeline.read_text(encoding="utf-8"))
    except Exception as exc:
        messages.append(f"shared/pipeline.yaml invalid: {exc}")
        return
    if not isinstance(data, dict) or "stages" not in data:
        messages.append("shared/pipeline.yaml has no stages")
        return
    stages = data["stages"]
    if not isinstance(stages, list) or not stages:
        messages.append("shared/pipeline.yaml stages is empty")
        return
    stage_ids = [s.get("id") for s in stages if isinstance(s, dict)]
    if len(stage_ids) != len(set(stage_ids)):
        messages.append("pipeline.yaml has duplicate stage ids")

    aliases = data.get("aliases", {}) or {}
    for alias, members in aliases.items():
        if not isinstance(members, list):
            messages.append(f"pipeline alias {alias!r} is not a list")
        for m in members:
            if m not in stage_ids:
                messages.append(f"pipeline alias {alias!r} references unknown stage {m!r}")

    def expand(nodes):
        out = []
        for n in nodes:
            if n in aliases:
                out.extend(expand(aliases[n]))
            else:
                out.append(n)
        return out

    for s in stages:
        if not isinstance(s, dict):
            messages.append("pipeline.yaml stage is not a mapping")
            continue
        for field in ["id", "activation_rule", "owner", "input", "output",
                      "requires", "order_after", "execution_mode", "frequency",
                      "suppression_condition", "runtime_cost_class"]:
            if field not in s:
                messages.append(f"pipeline stage {s.get('id', '?')} missing {field}")
        for key in ("requires", "order_after"):
            for pred in s.get(key, []):
                if pred not in stage_ids:
                    messages.append(f"pipeline stage {s.get('id')} has unknown {key} stage {pred!r}")
                if pred == s.get("id"):
                    messages.append(f"pipeline stage {s.get('id')} lists itself in {key}")

    # hard-dependency closure: every profile must satisfy all requires
    profiles = data.get("profiles", {}) or {}
    if not profiles:
        messages.append("pipeline.yaml has no activation profiles")
    for pname, profile in sorted(profiles.items()):
        if not isinstance(profile, list):
            messages.append(f"profile {pname!r} is not a list")
            continue
        active = expand(profile)
        # required closure (transitive)
        changed = True
        while changed:
            changed = False
            for s in stages:
                sid = s.get("id")
                if sid not in active:
                    continue
                for req in s.get("requires", []):
                    if req not in active:
                        active.append(req)
                        changed = True
        missing = set()
        for s in stages:
            sid = s.get("id")
            if sid not in active:
                continue
            for req in s.get("requires", []):
                if req not in active:
                    missing.add(req)
        if missing:
            messages.append(f"profile {pname!r} violates hard dependencies: missing {sorted(missing)}")

    # order_after DAG + terminal sispis
    indeg = {sid: 0 for sid in stage_ids}
    edges: Dict[str, List[str]] = {sid: [] for sid in stage_ids}
    for s in stages:
        sid = s.get("id")
        for pred in s.get("order_after", []):
            if pred in stage_ids and pred != sid:
                edges[pred].append(sid)
                indeg[sid] += 1
    queue = collections.deque([sid for sid, d in indeg.items() if d == 0])
    order = []
    while queue:
        cur = queue.popleft()
        order.append(cur)
        for nxt in edges[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(stage_ids):
        cycles = [sid for sid, d in indeg.items() if d > 0]
        messages.append(f"pipeline order_after DAG has a cycle involving: {cycles}")
    sispis_out = edges.get("sispis", [])
    if sispis_out:
        messages.append(f"pipeline DAG: sispis should be terminal but has successors {sispis_out}")


def validate_sispis_owns_calibration(messages: List[str]) -> None:
    import yaml
    text = (ROOT / "SISPIS" / "SKILL.md").read_text(encoding="utf-8")
    # SISPIS must reference shared/integration.md as canonical mapping owner
    if "shared/integration.md" not in text:
        messages.append("SISPIS/SKILL.md does not reference shared/integration.md as canonical mapping")
    # SISPIS must reference its own calibration table and describe ingestion
    if "signal-calibration.yaml" not in text:
        messages.append("SISPIS/SKILL.md does not reference its own signal-calibration.yaml")
    if "cause_id" not in text:
        messages.append("SISPIS/SKILL.md missing cause_id dedup in ingestion description")
    # Upstream protocol files must not claim to own SISPIS delta tables
    for skill in ["OWL", "FUSE", "FLOW", "WARD", "ANCHOR"]:
        skill_text = (ROOT / skill / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        if "SISPIS Entropy Mapping" in skill_text or "SISPIS Delta Mapping" in skill_text:
            messages.append(f"{skill}/SKILL.md still owns a SISPIS delta table")

    # Calibration table: every registry signal must have an entry; unknown
    # signal_types would silently contribute nothing at runtime.
    cal_path = ROOT / "SISPIS" / "references" / "signal-calibration.yaml"
    if not cal_path.exists():
        messages.append("Missing SISPIS/references/signal-calibration.yaml (SISPIS-owned scoring table)")
        return
    try:
        calibration = yaml.safe_load(cal_path.read_text(encoding="utf-8"))
    except Exception as exc:
        messages.append(f"SISPIS/references/signal-calibration.yaml invalid: {exc}")
        return
    if not isinstance(calibration, dict) or not isinstance(calibration.get("signals"), dict):
        messages.append("SISPIS/references/signal-calibration.yaml has no signals mapping")
        return
    # every signal must specify at least one calibration effect
    for sid, entry in calibration["signals"].items():
        if not isinstance(entry, dict):
            messages.append(f"SISPIS calibration entry {sid} is not a mapping")
            continue
        if "entropy" not in entry and "intent_weight" not in entry and "minimum_mode" not in entry:
            messages.append(f"SISPIS calibration entry {sid} has no calibration effect")
    signal_registry = json.loads((ROOT / "shared" / "signal-registry.json").read_text(encoding="utf-8"))
    reg_ids = {s["id"] for s in signal_registry["signals"]}
    for sid in sorted(reg_ids):
        if sid not in calibration["signals"]:
            messages.append(f"signal {sid} has no SISPIS calibration entry")
    for sid in sorted(set(calibration["signals"]) - reg_ids):
        messages.append(f"SISPIS calibration entry {sid} is not a registered signal")

    # Regression guard: the old upstream delta math must not appear anywhere
    # outside the SISPIS-owned calibration table. The calibration entry
    # vocabulary (intent_weight as data) is allowed; formula use is not.
    for skill in ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD"]:
        for path in sorted((ROOT / skill).rglob("*")):
            if not path.is_file() or not path.suffix.lower() in {".md", ".yaml", ".yml"}:
                continue
            t = path.read_text(encoding="utf-8", errors="replace")
            if "intent_weight" in t or "entropy_delta" in t or "E_adjusted" in t or "W_adjusted" in t:
                messages.append(f"Upstream SISPIS math still present in {rel(path)}")
    # root CLAUDE.md and README must not carry the stale protocol either
    for doc in ["CLAUDE.md", "README.md"]:
        t = (ROOT / doc).read_text(encoding="utf-8", errors="replace")
        for stale in ["highest-severity delta", "severity: 1.2", "severity: 1.5", "entropy_delta", "intent_weight"]:
            if stale in t:
                messages.append(f"{doc} still contains stale SISPIS scoring: {stale!r}")


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
    if "DOX is not a SISPIS upstream source" not in text:
        messages.append("shared/integration.md does not exclude DOX from SISPIS upstream signals")
    if "signal.schema.json" not in text:
        messages.append("shared/integration.md does not reference the canonical signal envelope schema")
    if "signal_id" not in text or "cause_id" not in text:
        messages.append("shared/integration.md does not define the canonical signal envelope fields")


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
    """Scenario manifest validation on the two-layer model:
    expected_findings carry the LOCAL signal type + LOCAL weight, plus an
    expected_emission in the canonical envelope (signal_type + severity) that
    must be registered in shared/signal-registry.json. Validates both layers;
    no hand-written duplicate registry of old numeric weights."""
    import json as _json
    path = ROOT / "shared" / "scenarios.json"
    if not path.exists():
        messages.append("Missing shared/scenarios.json")
        return
    try:
        manifest = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        messages.append(f"Invalid scenario manifest {rel(path)}: {exc}")
        return
    scenarios = manifest.get("scenarios") if isinstance(manifest, dict) else None
    if not isinstance(scenarios, list) or not scenarios:
        messages.append("Scenario manifest has no scenarios")
        return

    try:
        registry = _json.loads((ROOT / "shared" / "signal-registry.json").read_text(encoding="utf-8"))
    except Exception:
        registry = {"signals": []}
    reg_ids = {s["id"] for s in registry.get("signals", [])}

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
        findings = scenario.get("expected_findings", [])
        if not isinstance(findings, list):
            messages.append(f"{where} expected_findings is not a list")
            continue
        for fi, finding in enumerate(findings):
            fw = f"{where}.expected_findings[{fi}]"
            if not isinstance(finding, dict):
                messages.append(f"{fw} is not a mapping")
                continue
            local_type = finding.get("local_type")
            local_weight = finding.get("local_weight")
            if not local_type or not isinstance(local_weight, (int, float)):
                messages.append(f"{fw} missing local_type/local_weight (local layer)")
                continue
            emission = finding.get("emission")
            if not isinstance(emission, dict):
                messages.append(f"{fw} missing expected emission (canonical layer)")
                continue
            sig_type = emission.get("signal_type")
            source = emission.get("source")
            if sig_type not in reg_ids:
                messages.append(f"{fw} emission {sig_type!r} is not a registered canonical signal")
            if source and not str(sig_type).startswith(f"{source}."):
                messages.append(f"{fw} emission source {source!r} does not match signal_type {sig_type!r}")
            if emission.get("severity") not in {"low", "medium", "high"}:
                messages.append(f"{fw} emission has invalid severity {emission.get('severity')!r}")

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


def validate_statework_manifests(messages: List[str]) -> None:
    """Optional cross-tree check: if CognitiveStateWork exists, every manifest
    in its registry validates against the manifest schema."""
    import yaml

    cow_root = ROOT.parent / "CognitiveStateWork"
    if not cow_root.exists():
        return
    schema_path = cow_root / "schemas" / "statework-manifest.schema.json"
    registry_path = cow_root / "registry.yaml"
    if not schema_path.exists() or not registry_path.exists():
        messages.append("CognitiveStateWork missing schemas/statework-manifest.schema.json or registry.yaml")
        return
    import jsonschema
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except Exception as exc:
        messages.append(f"CognitiveStateWork registry.yaml invalid: {exc}")
        return
    for name in registry.get("stateworks", []):
        manifest_path = cow_root / name / "manifest.yaml"
        if not manifest_path.exists():
            messages.append(f"CognitiveStateWork {name} has no manifest.yaml")
            continue
        try:
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            jsonschema.validate(instance=data, schema=schema)
        except Exception as exc:
            messages.append(f"CognitiveStateWork {name}/manifest.yaml invalid: {exc}")


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


def validate_runtime_instruction_size(messages: List[str]) -> None:
    """Report the active runtime surface. The review's #13: compact kernels
    matter more than activation logic. Warnings (not hard failures) for any
    SKILL.md that regresses toward the old multi-thousand-word cores; the
    framework can then measure how small kernels can go without behavioral
    regression."""
    results = []
    for skill in EXPECTED_SKILLS + ["cogframe"]:
        path = ROOT / skill / "SKILL.md"
        if not path.exists():
            continue
        words = len(path.read_text(encoding="utf-8").split())
        results.append((words, skill))
    results.sort(reverse=True)
    total = sum(w for w, _ in results)
    report = ", ".join(f"{skill}={w}w" for w, skill in results)
    # informational report — shrinking kernels is the goal, measured here so
    # DigitalPsychology can empirically test smaller variants. The runtime
    # bundle emitted by resolve-runtime.py is the actual active surface
    # (stage activation + outputs + guard rules), not these authoring docs.
    print(f"[runtime surface] {report} | total={total}w")
    for words, skill in results:
        if words > 2500:
            print(f"  [note] {skill}/SKILL.md is {words} words — compact kernels preferred; "
                  "move exposition to references/ (not a gate)")
    # hard gate: nothing may regress past the former worst case (WARD 3,552 words)
    for words, skill in results:
        if words > 3552:
            messages.append(f"{skill}/SKILL.md exceeds former worst-case size ({words} words)")


def main() -> None:
    messages: List[str] = []
    validate_json_files(messages)
    validate_frontmatter_loadability(messages)
    validate_skill_count(messages)
    validate_integration_doc(messages)
    validate_adapter_source(messages)
    validate_signal_contracts(messages)
    validate_no_upstream_sispis_math(messages)
    validate_pipeline_manifest(messages)
    validate_sispis_owns_calibration(messages)
    validate_behavior_contracts(messages)
    validate_scenario_manifest(messages)
    validate_claude_contract(messages)
    validate_ward_recover(messages)
    validate_statework_manifests(messages)
    validate_adapter_generation(messages)
    validate_adapter_duplicate_hashes(messages)
    validate_runtime_instruction_size(messages)
    fail(messages)


if __name__ == "__main__":
    main()
