from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "final-gate.py"
spec = importlib.util.spec_from_file_location("cognitive_final_gate", GATE_PATH)
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)


def _contract(*, mandatory: bool = True) -> dict:
    return {
        "qualification_id": "external-behavior-v1",
        "mandatory_for_release": mandatory,
        "result_contract": {
            "required_fields": ["qualification_id", "status", "completed_at"],
            "status_values": ["not_run", "pass", "fail", "blocked"],
        },
    }


def _result(status: str) -> dict:
    return {
        "qualification_id": "external-behavior-v1",
        "status": status,
        "completed_at": None,
    }


@pytest.mark.parametrize("status", ["not_run", "fail", "blocked"])
def test_mandatory_qualification_must_pass_for_release(status: str) -> None:
    with pytest.raises(RuntimeError, match=f"mandatory qualification is {status}"):
        gate.validate_qualification_result(
            _contract(), _result(status), result_relative="qualification.json")


def test_passed_mandatory_qualification_is_accepted() -> None:
    gate.validate_qualification_result(
        _contract(), _result("pass"), result_relative="qualification.json")


def test_nonmandatory_not_run_result_is_valid() -> None:
    gate.validate_qualification_result(
        _contract(mandatory=False), _result("not_run"),
        result_relative="qualification.json")


def test_unknown_result_status_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="invalid status"):
        gate.validate_qualification_result(
            _contract(), _result("waived"), result_relative="qualification.json")


def test_missing_release_policy_is_rejected() -> None:
    contract = _contract()
    del contract["mandatory_for_release"]
    with pytest.raises(RuntimeError, match="release policy"):
        gate.validate_qualification_result(
            contract, _result("pass"), result_relative="qualification.json")
