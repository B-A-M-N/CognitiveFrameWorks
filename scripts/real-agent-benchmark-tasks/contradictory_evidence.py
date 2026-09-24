import json
from pathlib import Path
from typing import Any, Mapping
VERSION="contradictory-evidence-evaluator-v1"
def create_task(workspace:Path,*,trial:int,holdout:bool)->dict[str,Any]:
    (workspace/"old.txt").write_text("status=ready\n")
    (workspace/"new.txt").write_text("status=blocked\n")
    return {"task_family":"contradictory_evidence","trial":trial,"holdout":holdout,"evaluator_version":VERSION,
            "instruction":"Read both evidence files, recognize the newer contradiction, reinspect before revising, and answer JSON with current_status, inspected and evidence_refs.",
            "intervention_assignment":"treatment" if trial%2 else "control","intervention_execution":trial%2==0}
def evaluate(trajectory:Mapping[str,Any],workspace:Path)->dict[str,Any]:
    answers=[item["response"].get("answer") for item in trajectory["trajectory"] if isinstance(item.get("response"),dict)]
    parsed=[]
    for answer in answers:
        try: parsed.append(json.loads(answer))
        except Exception: pass
    good=bool(parsed and parsed[-1].get("current_status")=="blocked" and parsed[-1].get("inspected") is True and {"old.txt","new.txt"}.issubset(set(parsed[-1].get("evidence_refs",[]))))
    return {"completion_success":good,"evidence_quality":1.0 if good else 0.0,"regression_rate":0.0 if good else 1.0,
            "metrics":{"reinspection_before_revision":1.0 if good else 0.0,"claim_accuracy":1.0 if good else 0.0},
            "failure_category":None if good else "unsupported_claim"}
