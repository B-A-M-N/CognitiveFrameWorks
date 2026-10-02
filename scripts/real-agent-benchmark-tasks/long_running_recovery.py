import json
from pathlib import Path
from typing import Any,Mapping
VERSION="long-running-recovery-evaluator-v1"
def create_task(workspace:Path,*,trial:int,holdout:bool)->dict[str,Any]:
    (workspace/"journal.ndjson").write_text('{"op":"charge","id":"x","state":"committed"}\n{"op":"notify","id":"n","state":"pending"}\n')
    return {"task_family":"long_running_recovery","trial":trial,"holdout":holdout,"evaluator_version":VERSION,
            "instruction":"Recover after restart from journal.ndjson. Do not repeat charge x. Answer JSON with recovered_operation, repeat_side_effects and pending."}
def evaluate(trajectory:Mapping[str,Any],workspace:Path)->dict[str,Any]:
    answers=[]
    for item in trajectory["trajectory"]:
        try: answers.append(json.loads(item["response"].get("answer")))
        except Exception: pass
    a=answers[-1] if answers else {}
    good=a.get("recovered_operation")=="notify" and int(a.get("repeat_side_effects",99))==0 and a.get("pending") is True
    return {"completion_success":good,"evidence_quality":1.0 if good else 0.0,"regression_rate":0.0 if good else 1.0,
            "metrics":{"restart_state_accuracy":float(good),"duplicate_side_effect_count":int(a.get("repeat_side_effects",0))},
            "failure_category":None if good else "duplicate_side_effect"}
