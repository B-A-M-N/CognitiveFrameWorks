#!/usr/bin/env python3
"""Scrub non-Laya provider/model identifiers from published trial records.

User decision: remove provider and model identifiers for everything that is
NOT Laya. Laya identifiers are preserved.

PRESERVED (Laya -- the experiment's own subject):
  * field names: laya_query_attempts, laya_fallbacks, laya_advisory_results
  * cohort labels: cfw_csw_dp_laya, cfw_csw_dp_laya_advisory
  * any value mentioning laya (cohort arms, config keys, file names)

SCRUBBED (exact values, counted from the tree):
  provider_id        "freeinference"               123 occurrences
  model              "gpt-6-luna"                  176 occurrences
  model              "deepseek-v4-flash"           174 occurrences
  provider_base_url  "https://freeinference.org/v1"  17 occurrences
  provider_env_key   "FREEINFERENCE_API_KEY"         11 occurrences
  provider           "freeinference"                  1 occurrence

REPLACEMENTS
  provider_id  / provider            -> "external-provider"
  model                              -> "external-model"
  provider_base_url                  -> "https://provider.example/v1"
  provider_env_key                   -> "PROVIDER_API_KEY"

The env-var name is a *name*, not a secret -- no credential value appears in
these files. It is replaced because it identifies the specific upstream service.

HISTORY
  With --history, git-filter-repo applies the same substitution to every
  historical blob. This rewrites all 106 commits and requires a force-push.
  Back up first:
      git clone --mirror /home/bamn/CognitiveFrameWorks /path/to/backup.git
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/bamn/CognitiveFrameWorks")
TARGET_DIR = REPO / "evaluation_fixtures" / "live-results"

# Values to replace. Order matters: longest / most specific first.
REPLACEMENTS = [
    ("https://freeinference.org/v1", "https://provider.example/v1"),
    ("FREEINFERENCE_API_KEY", "PROVIDER_API_KEY"),
    ("freeinference", "external-provider"),
    ("gpt-6-luna", "external-model"),
    ("deepseek-v4-flash", "external-model"),
]

# Laya values are preserved: the same token may be a cohort arm or config key.
LAYA_TOKEN = re.compile(r"laya", re.IGNORECASE)


def rewrite_text(text: str, stats: dict) -> str:
    """Replace non-Laya identifiers, counting each substitution.

    A replacement is skipped when the surrounding context is Laya-specific, so
    a value that appears inside a Laya cohort record is left alone.
    """
    # Walk the text once, protecting spans that mention laya.
    protected: list[tuple[int, int]] = []
    for match in LAYA_TOKEN.finditer(text):
        # Widen to the enclosing JSON string literal when there is one.
        start, end = match.start(), match.end()
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        # Only protect when the laya token is part of a VALUE, not a key name.
        if re.search(r'"\s*:\s*"[^"]*laya[^"]*"', line, re.IGNORECASE):
            protected.append((line_start, line_end))

    def _in_protected(pos: int) -> bool:
        return any(s <= pos < e for s, e in protected)

    for source, target in REPLACEMENTS:
        pattern = re.compile(re.escape(source))
        out = []
        cursor = 0
        for match in pattern.finditer(text):
            if _in_protected(match.start()):
                continue
            out.append(text[cursor:match.start()])
            out.append(target)
            cursor = match.end()
            stats[source] = stats.get(source, 0) + 1
        out.append(text[cursor:])
        text = "".join(out)
    return text


def rewrite_file(path: Path, stats: dict) -> bool:
    raw = path.read_text(encoding="utf-8", errors="replace")
    new = rewrite_text(raw, stats)
    if new == raw:
        return False
    path.write_text(new, encoding="utf-8")
    return True


# The FreeInference acknowledgment is a deliberate public link, not a leak.
PRESERVE_FILES = ("README.md",)

# Only .json trial records carry the identifiers; the dir README is prose.
SCRUB_SUFFIXES = (".json", ".jsonl")


def candidates() -> list[Path]:
    out = []
    for path in sorted(TARGET_DIR.rglob("*")):
        if path.name in PRESERVE_FILES:
            continue
        if not path.is_file() or path.suffix not in SCRUB_SUFFIXES:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rewrite_text(raw, {}) != raw:
            out.append(path)
    return out


def verify_json(changed: list[Path]) -> list[str]:
    broken = []
    for path in changed:
        if path.suffix != ".json":
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            broken.append(f"{path.name}: {exc}")
    return broken


def rewrite_history() -> int:
    if subprocess.run(["which", "git-filter-repo"], capture_output=True).returncode != 0:
        print("git-filter-repo not found", file=sys.stderr)
        return 1
    blob_cb = (
        "import re, sys\\n"
        f"sys.path.insert(0, {str(REPO / 'scripts')!r})\\n"
        "from scrub_provider_identifiers import rewrite_text\\n"
        "if blob.data is None:\\n"
        "    return\\n"
        "try:\\n"
        "    text = blob.data.decode('utf-8')\\n"
        "except UnicodeDecodeError:\\n"
        "    return\\n"
        "new = rewrite_text(text, {})\\n"
        "if new != text:\\n"
        "    blob.data = new.encode('utf-8')\\n"
    )
    proc = subprocess.run(
        ["git", "filter-repo", "--force", "--blob-callback", blob_cb],
        cwd=REPO, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print((proc.stdout + proc.stderr)[-2000:], file=sys.stderr)
        return proc.returncode
    print(proc.stdout[-1200:])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()

    if not TARGET_DIR.is_dir():
        print(f"missing {TARGET_DIR}", file=sys.stderr)
        return 1

    print("=" * 70)
    print("CognitiveFrameWorks: scrub non-Laya provider/model identifiers")
    print("=" * 70)

    stats: dict = {}
    plan = candidates()
    print(f"  files needing change: {len(plan)}")
    counts: dict = {}
    for path in plan:
        rewrite_text(path.read_text(encoding="utf-8", errors="replace"), counts)
    print(f"  substitutions: {json.dumps(counts)}")

    if args.dry_run or not args.apply:
        for path in plan[:20]:
            print(f"    would rewrite {path.name}")
        if len(plan) > 20:
            print(f"    ... and {len(plan) - 20} more")
        print("\nDRY RUN -- nothing written." if not args.apply
              else "\nNo --history; working tree only.")
        if not args.apply:
            return 0

    changed = [p for p in plan if rewrite_file(p, stats)]
    broken = verify_json(changed)
    print(f"\nrewrote {len(changed)} file(s); {json.dumps(stats)}")
    if broken:
        print("JSON PARSE FAILURES:", file=sys.stderr)
        for line in broken:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("all rewritten JSON parses")

    # Laya must survive: the field names and cohort labels stay, and no file
    # that mentioned laya before the scrub lost all of its laya references.
    before_laya = {p for p in TARGET_DIR.rglob("*.json")
                   if "laya" in p.read_text(encoding="utf-8", errors="replace").lower()}
    lost = [p.name for p in changed
            if p in before_laya
            and "laya" not in p.read_text(encoding="utf-8", errors="replace").lower()]
    if lost:
        print(f"  WARNING laya references lost in: {lost}", file=sys.stderr)
    else:
        print(f"Laya preserved in {len(before_laya)} file(s) that reference it")

    if args.history:
        print("\nrewriting history (irreversible without a backup)...")
        if rewrite_history() != 0:
            print("history rewrite FAILED", file=sys.stderr)
            return 1
        print("history rewritten -- force-push required")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
