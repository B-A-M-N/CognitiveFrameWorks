"""CSW owns workflow semantics; CFW legacy APIs must stay thin adapters.

CFW's production resolution path goes through StateWorkProvider and CSW's
WorkflowPlanner. Compatibility entry points may remain for old callers, but
they must delegate to that provider instead of carrying another algorithm.

These tests prove the wrappers stay deprecated and the production path does
not route through them.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DUPLICATE_ENTRYPOINTS = {
    "match_statework": ROOT / "scripts" / "resolver" / "statework_matching.py",
    "match_stateworks": ROOT / "scripts" / "resolver" / "statework_matching.py",
    "plan_statework_dag": ROOT / "scripts" / "resolver" / "packet_planning.py",
    "plan_statework_chain": ROOT / "scripts" / "resolver" / "packet_planning.py",
}


@pytest.mark.parametrize("name,path", sorted(DUPLICATE_ENTRYPOINTS.items()))
def test_duplicate_entrypoint_is_deprecated(name, path):
    module = _load(f"legacy_{name}", path)
    entry = getattr(module, name)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            entry(None, ["domain"], None) if name.startswith("match") else entry(
                None, [], {})
        except Exception:
            # The wrapper must warn before delegating; argument shape is not
            # what this test is about.
            pass
    assert any(issubclass(item.category, DeprecationWarning) for item in caught), (
        f"{name} must warn that CSW owns this semantics")
    assert any("CSW" in str(item.message) for item in caught), (
        f"{name} must name CSW as the owner")


def test_production_resolution_path_does_not_import_the_duplicates():
    """The live resolver must reach workflow semantics only through CSW."""
    production = (
        ROOT / "scripts" / "resolver" / "task_workflows.py",
        ROOT / "scripts" / "resolver" / "workflow_composition.py",
        ROOT / "scripts" / "resolver" / "task_workflow_preparation.py",
    )
    for path in production:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                for alias in node.names:
                    imported.add(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        offenders = {
            name for name in imported
            if "statework_matching" in name or "packet_planning" in name
            or "statework_runtime" in name}
        assert not offenders, f"{path.name} imports a CFW-side workflow duplicate: {offenders}"


def test_compatibility_entrypoints_have_no_local_matching_or_dag_algorithm():
    matcher = (ROOT / "scripts" / "resolver" / "statework_matching.py").read_text()
    planner = (ROOT / "scripts" / "resolver" / "packet_planning.py").read_text()
    assert "_match_statework_impl" not in matcher
    assert "_plan_statework_dag_impl" not in planner
    assert "provider.eligible_routes" in matcher
    assert "provider.resolve_workflows" in planner


def test_runtime_facade_has_no_dead_yaml_import():
    """The compatibility facade carried two unused yaml imports."""
    source = (ROOT / "scripts" / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    yaml_imports = [
        node for node in tree.body
        if isinstance(node, ast.Import)
        and any(alias.name == "yaml" for alias in node.names)]
    assert not yaml_imports, (
        "runtime.py does not use yaml, so it should not import it at all")
    assert "yaml." not in source, "runtime.py does not use the yaml module"
