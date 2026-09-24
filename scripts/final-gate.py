#!/usr/bin/env python3
"""Release gate for a tagged, clean, isolated Cognitive Runtime checkout.

The gate is intentionally fail-closed.  It does not clean repositories,
remove files, or reuse a developer HOME.  The caller must provide a clean
tagged checkout (and clean sibling contract trees) before this script runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed in {root}: "
                           f"{(result.stdout + result.stderr).strip()}")
    return result.stdout.strip()


def require_clean_tag(root: Path, expected_tag: str | None) -> str:
    if not (root / ".git").exists():
        raise RuntimeError(f"not a git checkout: {root}")
    dirty = git(root, "status", "--porcelain")
    if dirty:
        raise RuntimeError(f"checkout is dirty: {root}")
    if expected_tag:
        try:
            tagged_commit = git(root, "rev-parse", "--verify",
                                f"refs/tags/{expected_tag}^{{commit}}")
        except RuntimeError as exc:
            raise RuntimeError(f"{root} does not have requested tag {expected_tag!r}") from exc
        if tagged_commit != git(root, "rev-parse", "HEAD"):
            raise RuntimeError(f"{root} tag {expected_tag!r} does not point at HEAD")
        return expected_tag
    tags = [item for item in git(root, "tag", "--points-at", "HEAD").splitlines() if item]
    if not tags:
        raise RuntimeError(f"HEAD is not exactly tagged: {root}")
    tag = sorted(tags)[0]
    return tag


def run(root: Path, command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(command, cwd=root, env=env,
                            text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"gate command failed ({result.returncode}): {' '.join(command)}")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="required exact tag for all three trees")
    parser.add_argument("--statework-root", type=Path,
                        default=(Path(__file__).resolve().parents[2] / "CognitiveStateWorks" if (Path(__file__).resolve().parents[2] / "CognitiveStateWorks").exists() else Path(__file__).resolve().parents[2] / "CognitiveStateWork"))
    parser.add_argument("--digital-psychology-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "DigitalPsychology")
    args = parser.parse_args()
    cfw = Path(__file__).resolve().parents[1]
    roots = [cfw, args.statework_root.resolve(), args.digital_psychology_root.resolve()]
    tags = [require_clean_tag(root, args.tag) for root in roots]
    if len(set(tags)) != 1:
        raise RuntimeError(f"contract trees do not share one release tag: {tags}")
    source_manifest = {
        str(root): {"tag": tag, "commit": git(root, "rev-parse", "HEAD")}
        for root, tag in zip(roots, tags)
    }
    artifact_manifest = {
        "plugin.json": file_hash(cfw / "plugin.json"),
        "enforcement-capabilities.json": file_hash(
            cfw / "contracts" / "enforcement-capabilities.json"),
        "transition-contract.schema.json": file_hash(
            cfw / "contracts" / "transition-contract.schema.json"),
        "release-gates.json": file_hash(cfw / "release-gates.json"),
    }

    # Both temporary directories start empty.  The installer is responsible
    # for creating its selected registry; this gate must never pre-create it.
    with (tempfile.TemporaryDirectory(prefix="cognitive-final-home-") as home,
          tempfile.TemporaryDirectory(prefix="cognitive-final-xdg-") as xdg,
          tempfile.TemporaryDirectory(prefix="cognitive-final-state-") as state):
        home_path = Path(home)
        if any(home_path.iterdir()):
            raise RuntimeError("temporary HOME was not empty before installation")
        env = dict(os.environ)
        env.update({
            "HOME": home,
            "XDG_RUNTIME_DIR": xdg,
            "XDG_STATE_HOME": str(Path(state) / "state"),
            "DIGITALPSYCHOLOGY_STATE_ROOT": str(Path(state) / "dp"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        release_registry = str(home_path / ".release-host" / "skills")
        run(cfw, [sys.executable, str(cfw / "scripts" / "install-framework.py"),
                  "--registry", f"release-host={release_registry}",
                  "--statework-root", str(args.statework_root)], env)
        run(cfw, [sys.executable, str(cfw / "scripts" / "doctor.py"),
                  "--registry", f"release-host={release_registry}"], env)
        if not Path(release_registry).is_dir():
            raise RuntimeError("clean install did not create the selected registry")

        manifest_path = cfw / "release-gates.json"
        if not manifest_path.is_file():
            raise RuntimeError("authoritative release-gates.json is missing")
        run(cfw, [sys.executable, str(cfw / "scripts" / "acceptance-public-beta.py")], env)
    for root in roots:
        if git(root, "status", "--porcelain"):
            raise RuntimeError(f"gate mutated the supposedly clean checkout: {root}")
    print(f"FINAL_GATE=PASS tag={tags[0]}")
    print("FINAL_GATE_SOURCES=" + json.dumps(source_manifest, sort_keys=True))
    print("FINAL_GATE_ARTIFACTS=" + json.dumps(artifact_manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"FINAL_GATE=BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(1)
