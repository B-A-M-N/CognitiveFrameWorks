"""Keep CFW policy/runtime core independent of sibling apps and harnesses."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOTS = tuple(ROOT / "scripts" / name for name in (
    "runtime_contract", "runtime_host", "runtime_session", "runtime_start"))
CORE_FILES = tuple(sorted(
    path for directory in CORE_ROOTS for path in directory.rglob("*.py")
    if path.is_file())) + tuple(ROOT / "scripts" / name for name in (
        "runtime.py", "cognitive_runtime.py"))
FORBIDDEN_IMPORT_ROOTS = (
    "digital_psychology", "digital_psychology_adapter", "lib",
    "cognitive_statework", "cognitive_stateworks",
    "control_plane", "state_store", "transition_engine", "tuid",
    "workflow_authority", "workflow_controller", "workflow_engines",
    "codex_host", "codex_app_server_adapter", "codex_app_server_dp_bridge",
)
FORBIDDEN_PROTOCOL_MARKERS = (
    "Codex", "Claude", "Athena", "codex_host", "codex_app_server",
    "CODEX_HOME", ".codex", "thread/start", "turn/start", "item/tool/call",
)


def _import_targets(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[tuple[int, str]] = []
    imported_names = {"import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            imported_names.update(
                alias.asname or alias.name for alias in node.names
                if alias.name == "import_module")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            targets.append((node.lineno, node.module))
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            is_import_module = (
                isinstance(func, ast.Name) and func.id in imported_names
            ) or (isinstance(func, ast.Attribute) and func.attr == "import_module")
            is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
            target = node.args[0]
            if ((is_import_module or is_dunder_import)
                    and isinstance(target, ast.Constant)
                    and isinstance(target.value, str)):
                targets.append((node.lineno, target.value))
    return targets


def test_cfw_core_does_not_import_sibling_apps_or_harness_adapters():
    missing = [str(path.relative_to(ROOT)) for path in CORE_FILES if not path.is_file()]
    assert not missing, f"expected CFW core files are missing: {missing}"

    violations = []
    for path in CORE_FILES:
        for line, module in _import_targets(path):
            if any(module == root or module.startswith(root + ".")
                   for root in FORBIDDEN_IMPORT_ROOTS):
                violations.append(f"{path.relative_to(ROOT)}:{line}: {module}")
    assert not violations, (
        "CFW core imports an application or harness adapter:\n"
        + "\n".join(violations))


def test_cfw_core_does_not_embed_harness_protocol_names():
    violations = []
    for path in CORE_FILES:
        source = path.read_text(encoding="utf-8").casefold()
        for marker in FORBIDDEN_PROTOCOL_MARKERS:
            if marker.casefold() in source:
                violations.append(f"{path.relative_to(ROOT)}: {marker}")
    assert not violations, (
        "Codex protocol details leaked into CFW core:\n"
        + "\n".join(violations))
