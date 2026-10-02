import json
from pathlib import Path
from typing import Any,Mapping
VERSION="statework-transition-evaluator-v1"
def create_task(workspace:Path,*,trial:int,holdout:bool)->dict[str,Any]:
    (workspace/"contract.json").write_text(json.dumps({"initial":"A","transitions":{"A":["B"],"B":["C"]},"state":"A","revision":7}))
    return {"task_family":"statework_transition","trial":trial,"holdout":holdout,"evaluator_version":VERSION,
            "instruction":"Apply one legal transition from contract.json, respecting revision 7. Answer JSON with from,to,expected_revision and stale_conflict_handled."}
def evaluate(trajectory:Mapping[str,Any],workspace:Path)->dict[str,Any]:
    answers=[]
    for item in trajectory["trajectory"]:
        try: answers.append(json.loads(item["response"].get("answer")))
        except Exception: pass
    a=answers[-1] if answers else {}
    good=a.get("from")=="A" and a.get("to")=="B" and int(a.get("expected_revision",-1))==7 and a.get("stale_conflict_handled") is True
    return {"completion_success":good,"evidence_quality":1.0 if good else 0.0,"regression_rate":0.0 if good else 1.0,
            "metrics":{"legal_transition_rate":float(good),"revision_conflict_recovery":float(a.get("stale_conflict_handled") is True)},
            "failure_category":None if good else "illegal_transition"}
