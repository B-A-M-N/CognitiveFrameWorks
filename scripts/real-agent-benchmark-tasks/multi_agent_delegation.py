import json
from pathlib import Path
from typing import Any,Mapping
VERSION="multi-agent-delegation-evaluator-v1"
def create_task(workspace:Path,*,trial:int,holdout:bool)->dict[str,Any]:
    (workspace/"facts.json").write_text(json.dumps({"owner":"agent-a","evidence":"e1","handoff":"agent-b"}))
    return {"task_family":"multi_agent_delegation","trial":trial,"holdout":holdout,"evaluator_version":VERSION,
            "instruction":"Use two workers conceptually: assign ownership and handoff correctly, attribute evidence, and answer JSON with owner, delegate, evidence, handoff_completed.",
            "intervention_assignment":"treatment" if trial%2 else "control","intervention_execution":trial%2==0}
def evaluate(trajectory:Mapping[str,Any],workspace:Path)->dict[str,Any]:
    answers=[]
    for item in trajectory["trajectory"]:
        try: answers.append(json.loads(item["response"].get("answer")))
        except Exception: pass
    a=answers[-1] if answers else {}
    good=a.get("owner")=="agent-a" and a.get("delegate")=="agent-b" and a.get("evidence")=="e1" and a.get("handoff_completed") is True
    return {"completion_success":good,"evidence_quality":1.0 if good else 0.0,"regression_rate":0.0 if good else 1.0,
            "metrics":{"task_ownership_accuracy":float(a.get("owner")=="agent-a"),"evidence_attribution_accuracy":float(a.get("evidence")=="e1"),"handoff_completion":float(a.get("handoff_completed") is True)},
            "failure_category":None if good else "handoff_failure"}
