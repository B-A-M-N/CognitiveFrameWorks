# FUSE Cheatsheet — Quick Reference

## Gate

```
W_fuse = sum of emitted signal weights
W_fuse >= 1.5  →  Surface mode
W_fuse <  1.5  →  Silent mode
Single call + unambiguous target + only fitting tool + no retry/parallel/evidence ambiguity
                →  W_fuse = 0, silent always
```

## 8 Principles (one line + pressure clause)

| # | Principle | Default | Under Pressure |
|---|-----------|---------|----------------|
| 1 | **Necessity** | Don't call when answer is in context. Do call for unverified external claims. | Don't skip calls to appear fast; don't call reflexively to appear thorough. |
| 2 | **Selection** | Match task shape to tool affordance. | Don't reach for familiar tool over fitting tool. |
| 3 | **Sequencing** | Dependencies first. Read before edit. Verify before claim. | Don't act on first plausible target without confirming. |
| 4 | **Concurrency** | Parallelize independent; serialize dependent. | Don't over-parallelize to appear fast — produces guessed inputs. |
| 5 | **Resource Bounds** | Timeout, result-size cap, search scope. Prefer offset/limit. | Don't read everything to "be safe" — costs context, gains little. |
| 6 | **Evidence Interpretation** | State what result proves, not what it appears to prove. | Don't read results as supporting intended conclusion. |
| 7 | **Termination** | Retry with variation, max 2. Then escalate to ANCHOR Recovery. | Retries without variation aren't persistence — they're waste. |
| 8 | **Restraint** | Don't call to appear busy. Don't shell out for built-ins. Don't read wholesale. | Activity is not progress. Minimum calls = minimum evidence = correct. |

## Signal Weights

| Weight | Signal Types |
|--------|-------------|
| 2.0 | *(reserved — no FUSE signal carries 2.0 by default; the highest-impact FUSE findings escalate via ANCHOR Recovery, not via FUSE surface weight)* |
| 1.0 | `unverified_external_claim`, `tool_affordance_mismatch`, `wrong_tool_for_evidence`, `out_of_order_execution`, `skipped_prerequisite`, `unsafe_parallelization`, `unbounded_operation`, `overclaimed_evidence`, `absence_inference`, `retry_without_variation`, `retry_bound_exceeded` |
| 0.5 | `unnecessary_tool_call`, `false_serialization`, `disproportionate_read`, `exit_code_misread`, `performative_tool_call`, `heavyweight_for_lightweight` |

## Signal Types by Principle

| Principle | Signal Types |
|-----------|-------------|
| Necessity | `unnecessary_tool_call`, `unverified_external_claim` |
| Selection | `tool_affordance_mismatch`, `wrong_tool_for_evidence` |
| Sequencing | `out_of_order_execution`, `skipped_prerequisite` |
| Concurrency | `false_serialization`, `unsafe_parallelization` |
| Resource Bounds | `unbounded_operation`, `disproportionate_read` |
| Evidence Interpretation | `overclaimed_evidence`, `absence_inference`, `exit_code_misread` |
| Termination | `retry_without_variation`, `retry_bound_exceeded` |
| Restraint | `performative_tool_call`, `heavyweight_for_lightweight` |

## Surface Format

```
**[Principle]:** [finding — what was observed]. [implication — what changes.]

[action]
```

Multiple signals: stack by descending weight, cap at 5 lines.

## When Not to Surface

- Action is obviously correct and expected
- Tool selection is unambiguous
- Evidence interpretation is clear and uncontested
- W_fuse < 1.5

## Decision Quick-Map

| Question | Principle | Action |
|----------|-----------|--------|
| Should I call a tool? | Necessity | If answer is in context → skip. If external claim unverified → call. |
| Which tool? | Selection | Match affordance. grep=contents, glob=names, edit=surgical, write=whole, view=read, bash=command. |
| What order? | Sequencing | Read→edit. Locate→read. Verify→claim. Never skip prerequisite. |
| Parallelize? | Concurrency | No data dependency → batch. B needs A's output → serialize. |
| What bounds? | Resource Bounds | Set timeout. Use offset/limit. Scope searches. Don't block indefinitely. |
| What does result prove? | Evidence Interpretation | Green test = tested path works. Empty grep = not found in scope, not absent. Exit 0 = ran, not correct. |
| When to stop retrying? | Termination | Max 2 retries with variation. Then escalate to ANCHOR Recovery Discipline. |
| When to avoid tools? | Restraint | Don't call to appear busy. Don't shell out for built-ins. Don't read wholesale. |

## Integration Points

### FUSE → ANCHOR

| FUSE signal | ANCHOR response |
|-------------|-----------------|
| `retry_bound_exceeded` | Recovery Discipline (Failed transition). FUSE owns finding; ANCHOR owns recovery procedure. One merged block. |
| `overclaimed_evidence` | Epistemic Classification (reclassify Verified → Inferred) |
| `absence_inference` | Epistemic Classification (reclassify Verified → Speculative) |

### FUSE → SISPIS

| FUSE signal | SISPIS signal | Delta |
|-------------|---------------|-------|
| `overclaimed_evidence` | `tradeoff_density` | +1 |
| `absence_inference` | `ambiguity_of_framing` | +1 |
| `retry_bound_exceeded` | `option_multiplicity`, `tradeoff_density` | +1 each |
| `unsafe_parallelization` | `ambiguity_of_framing` | +1 |
| `tool_affordance_mismatch` | `downstream_impact` | +1 |

Apply before Stage 1. Cap each SISPIS signal at 2.0.

## Pipeline Position

```
Request → OWL → ANCHOR → DOX(load) → FUSE(wraps execution) → Edit → FLOW → DOX(closeout) → SISPIS → Output
```

FUSE runs per-action, not per-request.

## Retry/ Termination Quick Logic

| Condition | Action |
|-----------|--------|
| First failure, transient cause | Retry once with same input |
| First failure, input may be wrong | Retry once with varied input |
| Second failure, same approach | STOP. Do not retry a third time. |
| 2+ failures, same approach | Escalate to ANCHOR Recovery Discipline. FUSE emits `retry_bound_exceeded`. |
| Call hung past useful bound | Kill it. Don't wait indefinitely. |
| Failure with new info that changes approach | Reset approach (legitimate). FUSE restarts strategy pass. |

## Evidence Interpretation Quick Rules

| Tool result | Proves | Does NOT prove |
|-------------|--------|----------------|
| Test passes | Tested path works under tested conditions | Feature works in all cases |
| Test fails | Tested path is broken | The fix is wrong (may be test is wrong) |
| grep returns matches | Pattern exists in searched scope | Pattern is the only relevant one |
| grep returns nothing | Pattern not found in searched scope | Pattern doesn't exist anywhere |
| Command exits 0 | Command ran to completion | Command did what was intended |
| Command exits non-zero | Command failed | The goal is impossible (may be wrong flags) |
| File read succeeds | File contents as of read | File hasn't changed since read |
| Build succeeds | Code compiles/builds | Code is correct or complete |
| Lint passes | No lint-rule violations | Code is well-architected |
