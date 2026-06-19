# Silent Execution — "Read the config file"

Calibrates: single obvious tool call, nothing surfaces, clean execution.

---

## Input

> Read the database config in `/home/bamn/CognitiveFrameWorks/config/db.yml`.

---

## Strategy Pass

**Necessity:** The request asks to read a file whose contents are not in context. External state must be observed. Tool call warranted.
Signals: none.

**Selection:** The task is reading file contents. `view` is the fitting tool — reads with line numbers, supports offset/limit. `bash` (cat) would work but is a heavyweight substitute for a built-in. `grep` would search contents, not read them.
Signals: none.

**Sequencing:** Single call. No prerequisites — the path is given. No dependencies.
Signals: none.

**Concurrency:** Single call. No parallelization decision.
Signals: none.

**Resource Bounds:** File is a config file — expected small. Full read acceptable. If it were large, offset/limit would apply.
Signals: none.

**Evidence Interpretation:** (post-read) The result shows the file contents as of read time. The claim "this is the db config" is supported by the contents. No overclaim.
Signals: none.

**Termination:** Not applicable — no failure.
Signals: none.

**Restraint:** One call, producing needed evidence. No performative calls. No heavyweight-for-lightweight.
Signals: none.

---

## Gate

W_fuse = 0. Silent mode. Suppression condition applies: single call, unambiguous target, only fitting tool, no retry/parallel/evidence ambiguity.

---

## Output

[view call proceeds, returns contents]

No surface lines. No preamble. Action and result only.

---

## What This Calibrates

The suppression condition applies cleanly. FUSE confirms the call is necessary, the tool is correct, and the scope is bounded — all silently. The correct behavior is to proceed without narrating the strategy check.

Anti-pattern to avoid: "I'll read the config file using the view tool to examine its contents:" — this narrates what the action self-evidently does. Just call the tool.
