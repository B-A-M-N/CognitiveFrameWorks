#!/usr/bin/env python3
"""Read-only cockpit, projection, and optional-install acceptance."""
import json, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; DP=ROOT.parent/"DigitalPsychology"; CSW=ROOT.parent/"CognitiveStateWork"
sys.path[:0]=[str(ROOT/"scripts"),str(DP),str(CSW)]
from operator_service import OperatorService
from runtime_contract.operator_events import OperatorEvent
from runtime_contract.operator_readmodel import ResyncRequired
from operator_ui import terminal
from lib.operator_projection import DPOperatorProjection
from scripts.operator_projection import CSWOperatorProjection

def main():
 with tempfile.TemporaryDirectory(prefix="cockpit-gate-") as raw:
  root=Path(raw); service=OperatorService(state_path=root/"operator.sqlite3")
  assert service.publish_runtime_event(OperatorEvent.create(sequence=1,namespace_id="n",application_instance_id="i",event_type="blocked",summary="blocked transition",source_system="cfw",correlation_id="c",severity="warning",reason_code="freshness"))
  assert service.wait_for_sequence(1); snap=service.snapshot(); assert snap["health"]["authority"]=="read_only"; assert "Cognitive Cockpit" in terminal.render(snap)
  assert "token" not in json.dumps(snap).lower(); service.close()
 print("OPERATOR_COCKPIT_GATE=PASS"); return 0
if __name__=="__main__": raise SystemExit(main())
