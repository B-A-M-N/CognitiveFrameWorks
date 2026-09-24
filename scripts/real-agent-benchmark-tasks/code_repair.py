from pathlib import Path
from typing import Any, Mapping

VERSION = "code-repair-evaluator-v1"

def create_task(workspace: Path, *, trial: int, holdout: bool) -> dict[str, Any]:
    (workspace / "broken.py").write_text("def add(a, b):\n    return a - b\n")
    return {"task_family": "code_repair", "trial": trial, "holdout": holdout,
            "evaluator_version": VERSION,
            "instruction": "Repair broken.py so add returns the sum. Write fixed.py; do not modify other files.",
            "intervention_assignment": "treatment" if trial % 2 else "control",
            "intervention_execution": trial % 2 == 0}

def evaluate(trajectory: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    fixed = workspace / "fixed.py"
    unrelated = [p for p in workspace.iterdir() if p.name not in {"broken.py", "fixed.py"}]
    passed = fixed.is_file() and "return a + b" in fixed.read_text()
    return {"completion_success": passed, "evidence_quality": 1.0 if passed else 0.0,
            "regression_rate": 0.0 if passed else 1.0,
            "metrics": {"hidden_test_pass_rate": 1.0 if passed else 0.0,
                        "unrelated_change_count": len(unrelated)},
            "failure_category": None if passed else "hidden_test_failure"}
