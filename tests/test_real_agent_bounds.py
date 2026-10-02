from __future__ import annotations

import importlib.util
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "acceptance-real-agent.py"
spec = importlib.util.spec_from_file_location("acceptance_real_agent_bounds", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_budget_stops_model_calls_tokens_cost_and_time() -> None:
    budget = module.QualificationBudget(
        max_model_calls=2, max_elapsed_seconds=1,
        max_external_tokens=10, max_monetary_cost=2,
    )
    budget.record_response({"usage": {"input_tokens": 2, "output_tokens": 3, "cost": 0.5}})
    budget.check()
    budget.record_response({"usage": {"input_tokens": 2, "output_tokens": 3, "cost": 0.5}})
    try:
        budget.check()
    except RuntimeError as exc:
        assert "model-call" in str(exc)
    else:
        raise AssertionError("model-call budget was not enforced")
    token_bounded = module.QualificationBudget(
        max_model_calls=10, max_elapsed_seconds=10,
        max_external_tokens=4, max_monetary_cost=2,
    )
    token_bounded.record_response({"usage": {"input_tokens": 3, "output_tokens": 2}})
    try:
        token_bounded.check()
    except RuntimeError as exc:
        assert "external-token" in str(exc)
    else:
        raise AssertionError("external-token budget was not enforced")
    cost_bounded = module.QualificationBudget(
        max_model_calls=10, max_elapsed_seconds=10,
        max_external_tokens=10, max_monetary_cost=0.5,
    )
    cost_bounded.record_response({"usage": {"cost": 0.5}})
    try:
        cost_bounded.check()
    except RuntimeError as exc:
        assert "monetary-cost" in str(exc)
    else:
        raise AssertionError("monetary-cost budget was not enforced")
    bounded = module.QualificationBudget(
        max_model_calls=10, max_elapsed_seconds=0.001,
        max_external_tokens=10, max_monetary_cost=2,
    )
    time.sleep(0.002)
    try:
        bounded.check()
    except RuntimeError as exc:
        assert "elapsed" in str(exc)
    else:
        raise AssertionError("elapsed budget was not enforced")


def test_persistent_agent_close_terminates_descendant_process_group() -> None:
    marker = Path(tempfile.mkdtemp()) / "child.pid"
    code = f'''import os, time
child = os.fork()
if child == 0:
    open({str(marker)!r}, "w").write(str(os.getpid()))
    while True: time.sleep(1)
print(os.getpid(), flush=True)
'''
    agent = module.PersistentAgent([sys.executable, "-c", code])
    parent_pid = agent.process.pid
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    child_pid = int(marker.read_text())
    agent.close()
    deadline = time.monotonic() + 2
    def descendant_is_live() -> bool:
        stat_path = Path(f"/proc/{child_pid}/stat")
        if not stat_path.exists():
            return False
        try:
            fields = stat_path.read_text().split()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return len(fields) > 2 and fields[2] != "Z"
    while time.monotonic() < deadline and descendant_is_live():
        time.sleep(0.02)
    assert not descendant_is_live(), "agent descendant survived process-group cleanup"
