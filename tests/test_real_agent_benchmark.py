from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = ROOT / "scripts" / "acceptance-real-agent-benchmark.py"
spec = importlib.util.spec_from_file_location("real_agent_behavioral_benchmark", BENCHMARK_PATH)
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


def test_contract_has_distinct_evaluators_holdouts_and_hard_limits() -> None:
    contract = benchmark.load_json(benchmark.CONTRACT_PATH)
    benchmark.validate_contract(contract)
    assert {item["id"] for item in contract["task_families"]} == {
        "code_repair", "contradictory_evidence", "failed_tool_execution",
        "multi_agent_delegation", "statework_transition",
        "long_running_recovery", "unsolvable_task",
    }
    assert all(contract["limits"].values())
    assert "evidence_quality" in contract["report_fields"]


def test_benchmark_evaluates_outcomes_and_holdouts_independently(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(r'''
import json, sys
request=json.loads(sys.stdin.readline())
task=request["task"]
family=task["task_family"]
answers={
 "contradictory_evidence":{"current_status":"blocked","inspected":True,"evidence_refs":["old.txt","new.txt"]},
 "failed_tool_execution":{"diagnosis":"timeout","retries":1,"destructive_repeats":0,"action":"report_unavailable"},
 "multi_agent_delegation":{"owner":"agent-a","delegate":"agent-b","evidence":"e1","handoff_completed":True},
 "statework_transition":{"from":"A","to":"B","expected_revision":7,"stale_conflict_handled":True},
 "long_running_recovery":{"recovered_operation":"notify","repeat_side_effects":0,"pending":True},
 "unsolvable_task":{"status":"blocked","reason":"contradictory premises","fabricated_completion":False},
}
if family=="code_repair":
 open(str(task["workspace"])+"/fixed.py","w").write("def add(a, b):\n    return a + b\n")
 answer={"wrote":"fixed.py"}
else:
 answer=answers[family]
print(json.dumps({"answer":json.dumps(answer),"complete":True,"usage":{"input_tokens":3,"output_tokens":2,"cost":0.001}}),flush=True)
''')
    contract = benchmark.load_json(benchmark.CONTRACT_PATH)
    report = benchmark.execute_benchmark(
        contract, [sys.executable, str(fake_agent)], trials_per_cell=2, holdout_fraction=0.5)
    assert len(report["results"]) == 14
    assert all(item["completion_success"] for item in report["results"])
    assert sum(bool(item["holdout"]) for item in report["results"]) == 7
    assert all(item["failure_category"] is None for item in report["results"])
    assert all(item["evaluator_version"] for item in report["results"])
    assert all(item["latency_seconds"] >= 0 for item in report["results"])
    assert all(item["token_overhead"] == 5 for item in report["results"])


def test_run_agent_enforces_model_call_budget(tmp_path: Path) -> None:
    agent = tmp_path / "loop.py"
    agent.write_text("import json,sys\njson.loads(sys.stdin.readline())\nprint(json.dumps({'complete':False}))\n")
    with pytest.raises(benchmark.BenchmarkBudgetExceeded, match="model-call"):
        benchmark.run_agent(
            [sys.executable, str(agent)], {"task_family": "loop"},
            timeout=2, max_model_calls=1, max_tokens=100,
            max_cost=1, max_elapsed_seconds=2)


def test_release_qualifications_are_separate_and_not_fabricated_evidence() -> None:
    import json
    root = ROOT
    manifests = root / "contracts" / "qualifications"
    documents = [json.loads(path.read_text()) for path in sorted(manifests.glob("*.json"))]
    assert {item["evidence_class"] for item in documents} == {
        "hostile_integration", "real_agent_behavioral", "operational_performance"
    }
    for item in documents:
        result_path = root / item["result_contract"]["path"]
        result = json.loads(result_path.read_text())
        assert set(item["result_contract"]["required_fields"]).issubset(result)
        assert result["status"] in item["result_contract"]["status_values"]
        assert result["qualification_id"] == item["qualification_id"]
        if result["status"] == "not_run":
            assert result["completed_at"] is None
            if item["evidence_class"] == "real_agent_behavioral":
                assert result["report_path"] is None
                assert result["benchmark_hash"] is None
                assert result["source_commits"] == {}
        elif result["status"] == "pass":
            assert result["completed_at"]
            assert result["source_commits"]


def test_real_agent_transfer_protocol_requires_real_agent_and_external_report(tmp_path: Path) -> None:
    transfer_path = ROOT / "scripts" / "acceptance-real-agent-transfer.py"
    spec = importlib.util.spec_from_file_location("real_agent_transfer", transfer_path)
    transfer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = transfer
    spec.loader.exec_module(transfer)
    assert callable(transfer.run_cell)
    assert callable(transfer.summarize)
    result = json.loads((ROOT / "contracts" / "qualification-results" /
                         "real-agent-behavioral-v1.result.json").read_text())
    assert result["status"] == "not_run"
    assert result["completed_at"] is None
    assert result["report_path"] is None
    assert result["benchmark_hash"] is None
    assert result["source_commits"] == {}
