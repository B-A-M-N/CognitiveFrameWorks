#!/usr/bin/env python3
"""Scrub provider/model identifiers from the live-results prose index.

Option C: scrub identifier VALUES from the narrative, keep the causal
sentences that explain why a trial failed, and update links to artifacts
that were renamed for the same reason.

WHAT CHANGES (value substitution only)
  `gpt-6-luna`      -> `external-model-a`
  `deepseek-v4-flash` -> `external-model-b`
  freeinference     -> external-provider   (lowercase, in prose)
  FreeInference     -> External provider   (sentence case, in prose)
  https://freeinference.org/v1 -> https://provider.example/v1
  FREEINFERENCE_API_KEY       -> PROVIDER_API_KEY
  `--model deepseek-v4-flash`  -> `--model external-model-b`
  `--provider-id freeinference` -> `--provider-id external-provider`

WHAT IS PRESERVED
  * The causal explanation. "FreeInference returned `model not found`" becomes
    "The provider returned `model not found`" -- the WHY survives, only the
    name is generalized. Same for the `401` auth diagnostics.
  * The README.md at the repo root, whose FreeInference acknowledgment is a
    deliberate public credit and must not be generalized.
  * The final reproduction command block, which must remain executable. Its
    model/provider flags are generalized, so an operator must substitute a
    real model id before running it -- a note is added saying exactly that.

TWO DISTINCT MODEL NAMES
  `external-model-a` and `external-model-b` are used rather than one shared
  label, because the record's meaning depends on them being DIFFERENT models
  (line 39-41 explains a failure on one and success on another). Collapsing
  them would make that sentence incoherent.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

TARGET = Path(
    "/home/bamn/CognitiveFrameWorks/evaluation_fixtures/live-results/README.md")

# Ordered: multi-word / URL forms first so partial matches cannot win.
REPLACEMENTS = [
    ("https://freeinference.org/v1", "https://provider.example/v1"),
    ("FREEINFERENCE_API_KEY", "PROVIDER_API_KEY"),
    ("deepseek-v4-flash", "external-model-b"),
    ("gpt-6-luna", "external-model-a"),
    ("freeinference", "external-provider"),
    ("FreeInference returned", "the provider returned"),
    ("FreeInference", "the provider"),
]

RENAMES = [
    ("noncanonical-aegiskeys-freeinference-auth-20261006.json",
     "noncanonical-aegiskeys-provider-auth-20261006.json"),
    ("noncanonical-aegiskeys-freeinference-triad-final-20261006.json",
     "noncanonical-aegiskeys-provider-triad-final-20261006.json"),
]

NOTE = (
    "\n> **Note:** the model and provider identifiers above are generalized.\n"
    "> Substitute a real model id and provider before running this command.\n")


def scrub(text: str) -> str:
    for source, target in REPLACEMENTS:
        text = text.replace(source, target)
    for old, new in RENAMES:
        text = text.replace(old, new)
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    raw = TARGET.read_text(encoding="utf-8")
    new = scrub(raw)

    # The causal sentences must survive generalization. Check the SCRUBBED
    # text: against the raw these read as LOST purely because the provider name
    # in them was substituted.
    causal_markers = [
        "returned `model not found`",
        "returned `401`",
    ]
    print("causal explanations preserved (checked in the scrubbed result):")

    # The source wraps these across lines ("returned\n`model not found`"), so
    # collapse whitespace before matching. A literal substring check reports
    # LOST on text that is in fact present.
    flat = re.sub(r"\s+", " ", new)
    for marker in causal_markers:
        print(f"   [{'OK' if marker in flat else 'LOST'}] {marker}")

    # Every renamed artifact must be referenced by its new name.
    bad_links = [old for old, _ in RENAMES if old in new]
    print(f"   stale links to renamed files: {bad_links or 'none'}")

    changed = sum(1 for a, b in zip(raw.splitlines(), new.splitlines()) if a != b)
    print(f"   lines changed: {changed} / {len(raw.splitlines())}")

    if not args.apply:
        import difflib
        print("\n--- diff preview (first 30 lines) ---")
        for line in list(difflib.unified_diff(
                raw.splitlines(), new.splitlines(), lineterm=""))[:30]:
            print(line[:150])
        print("\nDRY RUN -- nothing written.")
        return 0

    # Add the operator note to the reproduction block.
    if NOTE.strip() and "generalized" not in new:
        new = new.replace(
            "Trace comparisons\nare descriptive associations, not causal estimates.",
            "Trace comparisons\nare descriptive associations, not causal estimates."
            + NOTE)
    TARGET.write_text(new, encoding="utf-8")
    print(f"\nwrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
