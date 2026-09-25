#!/usr/bin/env python3
"""Open the optional read-only Cognitive Cockpit."""
from __future__ import annotations
import argparse, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from operator_service import OperatorService
from operator_ui import terminal,browser
from runtime_contract.operator_readmodel import OperatorReadModel

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument("--web",action="store_true");p.add_argument("--port",type=int,default=8765);a=p.parse_args()
 state=Path.home()/".local/state/cognitive-cockpit/operator.sqlite3"
 service=OperatorService(state_path=state)
 if a.web: return browser.run(service,port=a.port)
 return terminal.run(service)
if __name__=="__main__": raise SystemExit(main())
