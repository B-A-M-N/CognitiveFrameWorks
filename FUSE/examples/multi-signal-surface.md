# Multi-Signal Surface — "Edit before read, wholesale read planned"

Calibrates: two signals of different weights stack to exactly 1.5, surface mode activates, signals ordered by descending weight before the action. Demonstrates the threshold boundary.

---

## Input

> Add a `last_login` field to the `User` model in `models.py` and regenerate the migration.

---

## Context

`models.py` is known to be ~2000 lines. The `User` class location is not yet known this session — no grep or view has located it. The agent has not read `models.py` in this session.

---

## Strategy Pass (before action)

**Necessity:** The request depends on the actual contents of `models.py` (class definition, field conventions, base class). None of that is in context. A tool call is required before editing.
Signals: none (necessity confirms a call is needed).

**Selection:** `grep` to locate `class User`, then `view` with offset/limit to read the class body, then `edit`. Correct affordances.
Signals: none.

**Sequencing:** The current plan starts with an `edit` against `models.py` before the file has been read this session. The edit depends on the exact text of the `User` class — editing without reading risks a failed match or a wrong-location edit.
Signal: `out_of_order_execution` (1.0) — editing before reading; the prerequisite read was skipped.

**Concurrency:** Single edit planned. No parallelization decision yet.
Signals: none.

**Resource Bounds:** The planned read is `view` on the full `models.py` (~2000 lines) to find the `User` class. The evidence needed is one class definition (~30 lines). Reading 2000 lines to locate 30 is disproportionate — `grep` for `class User` first, then `view` with offset/limit on the returned line.
Signal: `disproportionate_read` (0.5) — result size disproportionate to evidence needed.

**Evidence Interpretation:** (post-read, not yet run) Not applicable pre-execution.
Signals: none.

**Termination:** Not applicable.
Signals: none.

**Restraint:** The wholesale read is already flagged by Resource Bounds. No separate performative call.
Signals: none.

---

## Gate

W_fuse = 1.0 + 0.5 = 1.5 >= 1.5 → Surface mode.

Two signals, descending weight: `out_of_order_execution` (1.0) first, then `disproportionate_read` (0.5).

---

## Output

```
**Sequencing:** The edit is being attempted before models.py has been read this session. The edit target depends on the exact class definition — editing blind risks a failed match or a wrong-location change.
**Resource Bounds:** Reading all ~2000 lines of models.py to find one class is disproportionate to the evidence needed. Grep for the class first, then scoped view.

[action: grep "class User" in models.py → view offset at returned line → edit]
```

---

## What This Calibrates

Two independent findings from different principles can stack to cross the surface threshold even when neither would surface alone. A single 1.0 signal stays silent (1.0 < 1.5); a single 0.5 signal stays silent; together they surface (1.5 >= 1.5).

The signals are ordered by descending weight — the higher-impact finding (editing before reading, which can produce a wrong edit) precedes the lower-impact one (a wasteful read, which costs context but not correctness). Each line is independent: distinct finding, distinct implication, no merging across signals.

The fix is not "add more calls" — it is "reorder and scope." Grep locates, scoped view reads, edit applies. Three calls instead of one wholesale edit, each producing evidence the next depends on.

Anti-pattern to avoid: proceeding with the blind edit because "I'll fix it if the match fails." A failed edit match is a wasted cycle; a successful match against the wrong location is a silent bug. Read first.

---

## SISPIS Integration

- `out_of_order_execution` → `downstream_impact` +0.5
- `disproportionate_read` → `downstream_impact` +0.5

SISPIS receives +1.0 to `downstream_impact` total. Modest elevation; gate depends on base E. The output frames the sequencing risk without escalating to SCHEMA unless base E was already high.
