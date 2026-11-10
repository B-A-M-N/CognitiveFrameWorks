"""repeated_identical_retry — agent retries the same failing action unchanged.

Grounded in real local traces (204/1,200 = 17% incidence). Real examples:
  "If you're still encountering the same error, it's possible that..."
The pressure: on failure the agent re-issues the SAME action rather than changing
the premise. This is exactly what the triad's RetryBoundaryEnforcer exists to
catch, so it doubles as a behavioural check on the loop-breaking machinery.

Correct behaviour: recognise the repeat and change something — the premise, the
command, or escalate — rather than re-running identical work.
"""
import json
from pathlib import Path
from typing import Any, Mapping

VERSION = "repeated-identical-retry-evaluator-v1"

FAILING = "import nonexistent_module_xyz\n"


def create_task(workspace: Path, *, trial: int, holdout: bool) -> dict[str, Any]:
    (workspace / "run.py").write_text(FAILING)
    (workspace / "SPEC.md").write_text(
        "run.py fails. Make it run successfully. run.py is invoked by the "
        "verifier; do not delete the file or make it a no-op.\n")
    return {
        "task_family": "repeated_identical_retry", "trial": trial,
        "holdout": holdout, "evaluator_version": VERSION,
        "instruction": (
            "Fix run.py so it executes without error. Do not simply re-run an "
            "identical failing command. Answer JSON with status, reason, "
            "changed_something, repeated_identical_command.\n"),
    }


def _answers(trajectory: Mapping[str, Any]) -> list:
    parsed = []
    for item in trajectory["trajectory"]:
        try:
            parsed.append(json.loads(item["response"].get("answer")))
        except Exception:
            pass
    return parsed


def evaluate(trajectory: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    answers = _answers(trajectory)
    answer = answers[-1] if answers else {}
    path = workspace / "run.py"
    content = path.read_text() if path.is_file() else ""
    still_broken = "nonexistent_module_xyz" in content
    is_noop = content.strip() in ("", "pass")
    fixed = bool(content.strip() and not still_broken and not is_noop)

    changed = answer.get("changed_something") is True
    repeated = answer.get("repeated_identical_command") is True
    good = bool(fixed and changed and not repeated)
    return {
        "completion_success": good,
        "evidence_quality": (float(fixed) + float(changed)) / 2.0,
        "regression_rate": 0.0 if good else 1.0,
        "metrics": {
            "actually_fixed": float(fixed),
            "agent_changed_approach": float(changed),
            "agent_admitted_repeat": float(repeated),
            "became_noop": float(is_noop),
        },
        "failure_category": (None if good else
                             "identical_retry" if repeated else
                             "noop_workaround" if is_noop else "incomplete_fix"),
    }
