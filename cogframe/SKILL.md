---
name: cogframe
description: CognitiveFrameWorks pipeline dispatcher — routes a task to the minimal subset of the seven behavioral protocols (owl, anchor, dox, fuse, flow, ward, sispis) and enforces their execution order. Use at the start of any non-trivial engineering task - multi-step implementation, debugging session, refactor, tool-heavy work, or risky operations - to decide which protocols apply. Do not use for trivial one-line answers.
---

# cogframe — CognitiveFrameWorks Dispatcher

The seven protocols are designed as a pipeline, but skills trigger
individually. This dispatcher is the pipeline: consult it once at task
start, load ONLY the subset the task needs, and apply them in order.

## Routing Table

Match the task to the FIRST row that fits. Load the listed skills (each is
an installed skill: `owl`, `anchor`, `dox`, `fuse`, `flow`, `ward`,
`sispis`).

| Task shape | Load | Why |
|---|---|---|
| Quick question / explanation, no code edits | sispis | Output calibration only |
| Single-turn code advice | owl, sispis | Reasoning pass + calibrated answer |
| Multi-turn debugging | owl, anchor, fuse, sispis | + state continuity + evidence discipline |
| Implementation using tools/edits | owl, anchor, fuse, ward, sispis | + action gating |
| Performance / operational review | owl, flow, sispis | Drag evaluation is the point |
| Editing in a project with AGENTS.md | owl, anchor, dox, fuse, ward, sispis | Docs are a binding contract |
| Destructive commands, secrets, network mutations | ward (mandatory, whatever else loads) | Blast-radius gate |

Additionally load `flow` mid-task if the work touches: retry/backoff,
queues/concurrency, caches, startup cost, hot paths, N+1 I/O, or CI/dev
workflow friction.

## Execution Order

For whatever subset is loaded, apply in this order — later stages consume
earlier stages' findings, never the reverse:

```
owl (reason before implementing)
  → anchor (baseline state; checkpoint before risky steps)
  → dox (read AGENTS.md contract chain before editing)
  → fuse + ward (wrap EVERY tool/action decision: fuse picks, ward gates)
  → flow (only if a drag trigger fired)
  → sispis (calibrate the final response format)
```

## Hard Rules (apply even when only one skill is loaded)

1. Read code before claiming anything about it (owl: Reality).
2. State only what tool output proves, not what it suggests (fuse:
   Evidence Interpretation). A passing test proves the test passes.
3. Destructive/irreversible actions require ward's gate — no exceptions
   for "obviously safe" cases; those are where blast radius hides.
4. On a failed approach: reset to the last known-good state (anchor:
   Recovery Discipline). Do not stack patches on a broken attempt.
5. Scope stays within what the request implies (owl: Locality). Surface
   scope expansion; don't silently do it.
