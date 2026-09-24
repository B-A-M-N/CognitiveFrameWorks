#!/usr/bin/env python3
"""Run the authoritative three-system release gate manifest."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent
MANIFEST = ROOT / "release-gates.json"


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != "1.0.0" or manifest.get("mandatory") is not True:
        print("PUBLIC_BETA_GATE=FAIL manifest is not authoritative")
        return 1
    report = []
    for gate in manifest.get("gates", []):
        name = str(gate.get("name") or "")
        cwd = BASE / str(gate.get("cwd") or "")
        command = gate.get("command")
        if not name or not isinstance(command, list) or not command or not cwd.is_dir():
            report.append({"name": name, "status": "missing"})
            print(json.dumps(report, sort_keys=True))
            return 1
        executable = cwd / str(command[0])
        if not executable.is_file():
            report.append({"name": name, "status": "missing", "path": str(executable)})
            print(json.dumps(report, sort_keys=True))
            return 1
        env = dict(os.environ)
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        result = subprocess.run([sys.executable, *map(str, command)], cwd=cwd,
                                env=env, text=True, check=False)
        status = "pass" if result.returncode == 0 else "fail"
        report.append({"name": name, "status": status, "returncode": result.returncode})
        print(f"{name.upper()}={status.upper()}")
        if result.returncode:
            print(json.dumps({"manifest_version": manifest["manifest_version"],
                              "gates": report}, sort_keys=True))
            return result.returncode
    commits = {}
    for repo in (ROOT, BASE / "CognitiveStateWork", BASE / "DigitalPsychology"):
        commits[repo.name] = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    print(json.dumps({"manifest_version": manifest["manifest_version"],
                      "gates": report, "commits": commits}, sort_keys=True))
    print("PUBLIC_BETA_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
