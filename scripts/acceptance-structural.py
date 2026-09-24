#!/usr/bin/env python3
"""Guard-aware structural experiment with a guard-blind fake agent.

The fake agent submits the same unsafe request in every cohort and never
reads the treatment context.  Only the host handler changes the outcome.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DP_ROOT = ROOT.parent / "DigitalPsychology"
sys.path.insert(0, str(DP_ROOT))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cfw-structural-") as temp:
        root = Path(temp)
        os.environ["DIGITALPSYCHOLOGY_STATE_ROOT"] = str(root / "dp-state")
        os.environ["XDG_RUNTIME_DIR"] = str(root / "runtime")
        from lib import feedback_loop as dp
        registry = dp.GuardRegistry(default_to_trusted_state=True)
        guard = dp.Guard(
            id="G-structural-tool-gate", family="G-structural-tool-gate", version="1",
            status="candidate", target_behavior="unsafe write request",
            rule="The host blocks this binding during the structural experiment.",
            priority="P0", scope={"domains": ["debug"], "task_shapes": ["quick"]},
            rollout={"canary_task_ids": ["structural-treatment"]},
            enforcement={"mode": "tool_gate", "handler": "authority.boundaries",
                         "handler_version": "1", "parameters": {"blocked_bindings": ["write"]},
                         "fallback_prompt": None})
        registry.add(guard)
        registry.transition("G-structural-tool-gate", "experiment")
        receipts = dp.ReceiptRegistry(default_to_trusted_state=True)
        from lib.receipts import DEFAULT_PRODUCTION_CRITERION
        from lib.feedback_loop import TrialEvidence
        builder = dp.ReceiptBuilder(
            evaluator_version_hash="structural@1",
            guard_semantic_hash=dp.guard_semantic_hash(guard),
            criterion=DEFAULT_PRODUCTION_CRITERION)
        outputs = {}
        for prefix, policy, outcome in (
                ("b", "baseline", "FAIL"), ("i", "treatment", "PASS"),
                ("h", "holdout", "PASS")):
            for i in range(10):
                trial_id = f"{prefix}{i}"
                outputs[trial_id] = TrialEvidence(
                    trial_id=trial_id, trajectory_id=f"trajectory-{trial_id}",
                    probe_id=f"structural-probe-{trial_id}", probe_version="1",
                    policy_hash=policy, execution_policy_hash=policy,
                    session_id=f"session-{trial_id}", task_id=f"task-{trial_id}",
                    attempt_id="attempt-1", outcome=outcome,
                    host_evidence_ref=f"host-event-{trial_id}")
        validation = builder.build(
            guard_key=guard.key, kind="validation",
            baseline_trial_ids=tuple(f"b{i}" for i in range(10)),
            intervention_trial_ids=tuple(f"i{i}" for i in range(10)),
            holdout_trial_ids=tuple(f"h{i}" for i in range(10)),
            trial_outputs=outputs,
            baseline_policy_hashes=("baseline",),
            treatment_policy_hashes=("treatment",),
            holdout_policy_hashes=("holdout",),
            environment="structural-acceptance", harness="offline")
        receipts.add(validation)
        registry.transition("G-structural-tool-gate", "validated",
                            receipt_registry=receipts, receipt_id=validation.receipt_id)
        registry.transition("G-structural-tool-gate", "canary", receipt_registry=receipts)
        receipts.save()
        registry.save()
        export = load("structural_export", DP_ROOT / "scripts" / "export-guard-pack.py")
        if export.main() != 0:
            return 1
        resolver = load("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
        runtime = load("runtime", ROOT / "scripts" / "runtime.py")
        sys.modules["runtime"] = runtime
        sys.modules["resolve_runtime"] = resolver
        api = load("cognitive_runtime", ROOT / "scripts" / "cognitive_runtime.py")
        resolver.runtime_state_dir = lambda: root / "runtime" / "cognitiveframeworks"

        def fake_agent(request):
            host_runtime = api.CognitiveRuntime()
            session = host_runtime.begin_task(request)
            agent = host_runtime.agent_handle(session)
            # Deliberately identical across cohorts; no guard/context access.
            return agent.request_action({
                "tool_binding_id": "write",
                "arguments": {"target": "task-output"},
                "subject_ref": request.subject_ref,
            }), session

        control_request = resolver.TaskRequest(
            task_id="structural-control", application_id="cfw-structural",
            subject_ref="task:structural",
            shape="quick", domain_tags=("debug",))
        treatment_request = resolver.TaskRequest(
            task_id="structural-treatment", application_id="cfw-structural",
            subject_ref="task:structural",
            shape="quick", domain_tags=("debug",))
        control = resolver.resolve(control_request)
        treatment = resolver.resolve(treatment_request)
        control_decision, _control_session = fake_agent(control_request)
        treatment_decision, treatment_session = fake_agent(treatment_request)
        assert control_decision == runtime.GateDecision.ALLOW, control_decision
        assert treatment_decision == runtime.GateDecision.BLOCK, treatment_decision
        treatment_context = api.CognitiveRuntime().before_model_call(treatment_session)
        assert "The host blocks this binding" not in treatment_context
        assert treatment.guard_pack["structural_handlers"]
        print("guard-blind fake agent: control allowed, treatment structurally blocked")
        print("STRUCTURAL ACCEPTANCE PASSED")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
