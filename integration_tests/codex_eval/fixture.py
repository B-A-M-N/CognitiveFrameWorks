"""Disposable coding fixture, workspace tools, and host validators."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Mapping

from .common import CFW
from codex_app_server_adapter import CodexActionBinding, CodexValidatorBinding
from runtime_contract.actions import ActionDescriptor

FIXTURE = CFW / "evaluation_fixtures" / "codex-app-server-coding-v1"



def _load_contract() -> dict[str, Any]:
    value = json.loads((FIXTURE / ".cognitive-demo.json").read_text(encoding="utf-8"))
    if value.get("fixture_id") != "codex-app-server-coding-v1":
        raise RuntimeError("coding evaluation fixture identity mismatch")
    return value

def _materialize_workspace(workspace: Path) -> None:
    for name in (".cognitive-demo.json", "README.md", "src", "tests"):
        source = FIXTURE / name
        target = workspace / name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

def _workspace_path(workspace: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve(strict=False)
    resolved.relative_to(workspace.resolve())
    return resolved

def _source_digest(workspace: Path) -> str:
    digest = hashlib.sha256()
    source_root = workspace / "src"
    for path in sorted(source_root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(workspace).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()



def _tracked_files(workspace: Path) -> list[str]:
    result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=workspace, check=True, capture_output=True, text=True)
    paths = []
    for line in result.stdout.splitlines():
        if len(line) >= 4:
            paths.append(line[3:].split(" -> ")[-1])
    return sorted(set(paths))
