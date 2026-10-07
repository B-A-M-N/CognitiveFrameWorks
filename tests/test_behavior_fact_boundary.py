"""Keep host facts independent from the optional semantic-provider contract."""
from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FACTS_PATH = ROOT / "scripts" / "runtime_contract" / "behavior_facts.py"
PROVIDER_PATH = ROOT / "scripts" / "runtime_contract" / "system_one.py"
FACT_TYPES = {
    "ActivityFact", "AttemptSemanticDescriptor", "CriterionState",
    "WorkflowProgressFact",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_host_fact_types_are_defined_outside_system_one_provider_contract():
    fact_tree = _tree(FACTS_PATH)
    provider_tree = _tree(PROVIDER_PATH)
    fact_definitions = {
        node.name for node in fact_tree.body if isinstance(node, ast.ClassDef)
    }
    provider_definitions = {
        node.name for node in provider_tree.body if isinstance(node, ast.ClassDef)
    }
    assert FACT_TYPES <= fact_definitions
    assert not FACT_TYPES & provider_definitions

    provider_imports = [
        node.module for node in ast.walk(fact_tree)
        if isinstance(node, ast.ImportFrom)
    ]
    assert not any(module and module.endswith(("system_one", "protocol_artifact"))
                   for module in provider_imports)


def test_task_evidence_state_imports_provider_independent_facts():
    path = ROOT / "scripts" / "runtime_contract" / "task_evidence.py"
    tree = _tree(path)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "behavior_facts"
        for alias in node.names
    }
    assert {"ActivityFact", "AttemptSemanticDescriptor", "CriterionState",
            "WorkflowProgressFact"} <= imports


def test_metacognition_module_is_a_compatibility_shim():
    from runtime_contract.metacognition import MetacognitiveState
    from runtime_contract.task_evidence import TaskEvidenceState

    assert MetacognitiveState is TaskEvidenceState


def test_live_behavior_state_uses_the_task_evidence_contract():
    from runtime_session.state import BehaviorState
    from runtime_contract.task_evidence import TaskEvidenceState

    state = BehaviorState()
    field_names = {item.name for item in fields(BehaviorState)}
    assert "task_evidence" in field_names
    assert "metacognitive" not in field_names
    assert isinstance(state.task_evidence, TaskEvidenceState)
    assert state.metacognitive is state.task_evidence


def test_semantic_signal_state_is_legacy_only_and_outside_live_cfw_runtime():
    metacognition = ROOT / "scripts" / "runtime_contract" / "metacognition.py"
    legacy_signals = ROOT / "scripts" / "runtime_contract" / "legacy_system_one_signals.py"
    metacognition_tree = _tree(metacognition)
    legacy_tree = _tree(legacy_signals)
    legacy_types = {"SemanticSignals", "SemanticSignalRecord", "SemanticSignalStore"}

    metacognition_types = {
        node.name for node in metacognition_tree.body if isinstance(node, ast.ClassDef)
    }
    legacy_type_definitions = {
        node.name for node in legacy_tree.body if isinstance(node, ast.ClassDef)
    }
    assert not legacy_types & metacognition_types
    assert legacy_types <= legacy_type_definitions

    live_roots = (
        ROOT / "scripts" / "runtime_session",
        ROOT / "scripts" / "runtime_host",
        ROOT / "scripts" / "runtime_start",
    )
    for path in (ROOT / "scripts" / "runtime.py", *(
            candidate for root in live_roots for candidate in root.rglob("*.py"))):
        tree = _tree(path)
        imports = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(module and module.endswith((
            "legacy_system_one_signals", "behavioral_fusion", "system_one_state"))
            for module in imports), str(path.relative_to(ROOT))


def test_legacy_semantic_signal_artifact_round_trip():
    from runtime_contract.legacy_system_one_signals import SemanticSignalStore

    store = SemanticSignalStore()
    store.record(
        question_family="action_relation", value="advances_primary_goal",
        scenario="legacy-read", candidate_digest="a" * 64,
        metacognitive_revision=3, source_decision_id="decision-1")
    restored = SemanticSignalStore()
    restored.restore(store.snapshot())

    assert restored.current(metacognitive_revision=3).action_relation == (
        "advances_primary_goal")


def test_system_one_compatibility_imports_reexport_the_same_fact_types():
    from runtime_contract import behavior_facts, system_one

    for name in FACT_TYPES | {"WorkflowFact"}:
        assert getattr(system_one, name) is getattr(behavior_facts, name)
