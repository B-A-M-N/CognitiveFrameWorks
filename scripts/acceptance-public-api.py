#!/usr/bin/env python3
"""End-to-end smoke test using only the host/agent public runtime API."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cognitive_runtime import CognitiveRuntime
from resolve_runtime import AcceptanceCriterion, TaskGoalContract, TaskRequest
from runtime import GateDecision


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cfw-public-api-") as runtime_dir:
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir
        runtime = CognitiveRuntime(telemetry_disabled=True)
        request = TaskRequest(task_id="public-api", application_id="cfw-public-api",
                              shape="quick",
                              goal_contract=TaskGoalContract(
                                  objective="Verify the public runtime API can validate and finish a task.",
                                  deliverable_kind="code_behavior", work_mode="delivery",
                                  acceptance_criteria=(AcceptanceCriterion(
                                      "C1", "The host validates the completed task result.",
                                      "code_behavior"),)))
        session = runtime.begin_task(request)
        agent = runtime.agent_handle(session)
        host = runtime.host_handle(session)

        # The fake agent can request work but cannot produce its own evidence.
        assert agent.preview_output({"status": "draft"})["status"] == "draft"
        invocation_id = "public-boundary-invocation"
        task_evidence = host.after_tool(tool_type="test", result_class="pass",
                                        subject_ref="artifact:public", invocation_id=invocation_id,
                                        actual_result={"status": "pass"})
        decision = host.validate_completion_boundary(
            subject_ref="artifact:public", validator_id="completion-boundary-v1",
            invocation_id=invocation_id, result={"status": "pass"},
            criterion_evidence_refs={"C1": [task_evidence]})
        assert decision is GateDecision.ALLOW, decision
        finished = host.finish_task({"status": "complete"})
        assert finished["status"] == "complete"

    print("PUBLIC_API_ACCEPTANCE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
