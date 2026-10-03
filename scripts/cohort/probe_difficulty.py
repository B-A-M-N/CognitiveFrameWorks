#!/usr/bin/env python3
"""Measure whether a candidate task family discriminates against the baseline.

Run this BEFORE adding a family to the benchmark contract:

    python3 scripts/cohort/probe_difficulty.py path/to/candidate.py --trials 3

A family the unassisted baseline passes on every trial cannot show any
difference between configurations, so adding it would inflate the benchmark
without adding evidence. The first triad cohort saturated on five of seven
families for exactly this reason, and two candidates were discarded after this
probe reported SATURATES.

This measures the baseline only. It is a screening tool, not a gate: it says
whether a family is worth testing, and it costs inference, so it is not part of
the test suite.
"""
import argparse, importlib.util, json, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = str(ROOT / "scripts" / "cohort" / "agent_codex.py")


def load(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("modules", nargs="+")
    ap.add_argument("--trials", type=int, default=3)
    a = ap.parse_args()
    for path in a.modules:
        m = load(path)
        name = Path(path).stem
        wins = 0
        detail = []
        for t in range(a.trials):
            with tempfile.TemporaryDirectory() as raw:
                ws = Path(raw) / name / str(t)
                ws.mkdir(parents=True)
                task = m.create_task(ws, trial=t, holdout=False)
                task["workspace"] = str(ws)
                req = json.dumps({"task": {k: task[k] for k in
                        ("task_family", "instruction", "workspace")},
                        "turn": 0, "previous": []})
                try:
                    out = subprocess.run([sys.executable, ADAPTER], input=req,
                                         capture_output=True, text=True, timeout=400)
                    resp = json.loads(out.stdout.strip().splitlines()[-1])
                    ev = m.evaluate({"task": task, "trajectory":
                                     [{"turn": 0, "response": resp}]}, ws)
                    ok = bool(ev["completion_success"])
                    detail.append({k: ev["metrics"].get(k) for k in ev["metrics"]})
                except Exception as exc:
                    ok = False
                    detail.append({"error": f"{type(exc).__name__}: {exc}"[:90]})
                wins += ok
        verdict = "DISCRIMINATES" if wins < a.trials else "SATURATES (do not add)"
        print(f"{name:28s} baseline {wins}/{a.trials}  -> {verdict}")
        for d in detail:
            print("      ", json.dumps(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
