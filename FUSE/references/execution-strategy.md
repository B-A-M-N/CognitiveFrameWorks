# FUSE Execution Strategy — Integration Spec

## Signal Type Registry

Complete registry of all signal types, organized by principle. Multiple types per principle — a principle can emit any signal from its list in a single strategy pass.

### Necessity

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `unnecessary_tool_call` | 0.5 | A tool is about to be called to answer a question already resolved in current context — the call would only reconfirm established information |
| `unverified_external_claim` | 1.0 | A claim about external state (file contents, command output, API response) is being made or acted on without an observed tool result to back it this session |

### Selection

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `tool_affordance_mismatch` | 1.0 | A tool is being used outside its affordance — grep for filenames, bash for file edits, view where grep would locate, write where edit suffices |
| `wrong_tool_for_evidence` | 1.0 | A tool result is being trusted as evidence, but the tool used was not capable of producing that specific evidence (e.g., trusting a filename search to prove a function's behavior) |

### Sequencing

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `out_of_order_execution` | 1.0 | A tool call is being made before its prerequisite — editing before reading, claiming before verifying, building before writing |
| `skipped_prerequisite` | 1.0 | A prerequisite read or locate was skipped, and the current action depends on information that prerequisite would have provided |

### Concurrency

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `false_serialization` | 0.5 | Independent tool calls (no data dependency) are being issued serially when they could be batched in one message |
| `unsafe_parallelization` | 1.0 | Dependent calls are being parallelized — call B depends on call A's output but is being issued in the same batch, forcing B to guess at A's result |

### Resource Bounds

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `unbounded_operation` | 1.0 | A tool call has no implicit bound — reading a whole large file, searching the entire filesystem, running a command with no timeout or background strategy |
| `disproportionate_read` | 0.5 | A call's result size is disproportionate to the evidence needed — reading 500 lines to find one function, searching a whole repo to find one file |

### Evidence Interpretation

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `overclaimed_evidence` | 1.0 | A tool result is being interpreted more broadly than it warrants — a passing test treated as "feature works," a successful build treated as "code is correct" |
| `absence_inference` | 1.0 | An empty result is being treated as proof of absence — grep returning nothing treated as "the pattern doesn't exist," rather than "not found in the searched scope" |
| `exit_code_misread` | 0.5 | A command exit code is being misinterpreted — exit 0 treated as "did what was intended" rather than "ran to completion," or non-zero treated as "goal impossible" rather than "this invocation failed" |

### Termination

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `retry_without_variation` | 1.0 | The same tool call is being retried with identical input after a failure — retrying without varying the approach |
| `retry_bound_exceeded` | 1.0 | Retries have exceeded 2 without a strategy change. Escalation to ANCHOR Recovery Discipline is required. |

### Restraint

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `performative_tool_call` | 0.5 | A tool call is being made to demonstrate activity rather than to produce evidence — calling a tool to appear busy or thorough |
| `heavyweight_for_lightweight` | 0.5 | A heavyweight tool is being used where a lightweight one suffices — bash for file ops that edit/view/glob handle, reading a whole file when offset/limit gets the section |

---

## Weight Summary

| Weight | Signal Types |
|--------|-------------|
| 1.0 | `unverified_external_claim`, `tool_affordance_mismatch`, `wrong_tool_for_evidence`, `out_of_order_execution`, `skipped_prerequisite`, `unsafe_parallelization`, `unbounded_operation`, `overclaimed_evidence`, `absence_inference`, `retry_without_variation`, `retry_bound_exceeded` |
| 0.5 | `unnecessary_tool_call`, `false_serialization`, `disproportionate_read`, `exit_code_misread`, `performative_tool_call`, `heavyweight_for_lightweight` |

No FUSE signal carries 2.0 by default. The highest-impact FUSE findings (approach failure from retries, evidence misinterpretation) escalate through ANCHOR's Recovery Discipline and SISPIS's entropy elevation, not through FUSE surface weight. This keeps FUSE's gate from firing on routine execution choices while still escalating consequential findings through the downstream skills.

---

## Gate Threshold

```
W_fuse = sum of all emitted signal weights

W_fuse >= 1.5  →  Surface mode
W_fuse <  1.5  →  Silent mode
```

A single weight-1.0 signal does not surface (1.0 < 1.5). Two weight-1.0 signals surface (2.0 >= 1.5). Three weight-0.5 signals surface (1.5 >= 1.5). One weight-1.0 + one weight-0.5 surfaces (1.5 >= 1.5).

---

## Stacking Rules

Multiple signals can surface in a single response. Rules for stacking:

1. **Order by descending weight.** Higher-weight signals appear first.
2. **Suppress redundant signals from the same principle.** If `out_of_order_execution` surfaces, do not also surface `skipped_prerequisite` from the same observation.
3. **Don't merge finding + implication across signals.** Each signal line is independent.
4. **Cap at five surface lines.** Additional signals are available internally but not output.

---

## Suppression Conditions

Do not run the strategy pass (W_fuse = 0, silent) when all of the following are true:
- The action is a single tool call
- The target is unambiguous (known location, known file)
- The tool is the only one that fits the task
- No retry is involved
- No parallelization decision exists
- No evidence ambiguity exists

Examples: reading a file you've already located by path; editing a file you've already read this session; running a single known test command.

---

## Decision Trees

### Necessity Decision Tree

```
Is the claim about external state (file, command, API)?
├── No → answer from context. No tool needed.
└── Yes → has it been observed this session?
    ├── Yes → use prior observation. No tool needed.
    └── No → is it load-bearing for the current action?
        ├── No → proceed, but label as inferred.
        └── Yes → tool call required. Emit `unverified_external_claim` if skipped.
```

### Selection Decision Tree

```
What does the task need?
├── Find file by name/pattern → glob
├── Search file contents → grep
├── Read file contents → view (with offset/limit)
├── Surgical find-replace → edit
├── Overwrite/create whole file → write
├── Run a command → bash
├── Search code across repos → sourcegraph
├── Complex multi-step search → agent
└── List directory → ls

Wrong-tool check:
- Using grep for a filename? → switch to glob. Emit `tool_affordance_mismatch`.
- Using bash to edit a file? → switch to edit. Emit `tool_affordance_mismatch`.
- Using view to find where something is? → switch to grep first, then view. Emit `tool_affordance_mismatch`.
- Trusting a filename search to prove behavior? → Emit `wrong_tool_for_evidence`.
```

### Sequencing Decision Tree

```
Before editing:
1. Have I located the file? → glob/grep first
2. Have I read the relevant section? → view with offset/limit
3. Have I confirmed the exact text to replace? → view the surrounding context

Before claiming:
1. Have I run the verification? → test/build/lint
2. Have I interpreted the result correctly? → Evidence Interpretation principle

Before building/running:
1. Are dependencies in place? → check
2. Is the environment correct? → verify

Out-of-order check:
- Editing before reading → emit `out_of_order_execution`
- Claiming before verifying → emit `out_of_order_execution`
- Building before saving → emit `out_of_order_execution`
- Prerequisite skipped → emit `skipped_prerequisite`
```

### Concurrency Decision Tree

```
Are there multiple tool calls to make?
├── No → single call. No concurrency decision.
└── Yes → does any call depend on another's output?
    ├── No (all independent) → batch in one message. Emit `false_serialization` if serialized.
    └── Yes (dependencies exist) →
        Can the dependent calls be deferred until prerequisites return?
        ├── Yes → issue prerequisites first (batched if independent), then dependents
        └── No → emit `unsafe_parallelization`. Serialize.
```

### Resource Bounds Decision Tree

```
Is the read target large (>500 lines or unknown size)?
├── Yes → use offset/limit. Start with a scoped read. Emit `unbounded_operation` if reading whole.
└── No → full read acceptable.

Is the search scope the entire filesystem/repo?
├── Yes → narrow to likely directory. Emit `unbounded_operation` if unscoped.
└── No → proceed.

Might the command hang or run long?
├── Yes → use background execution or set a timeout. Emit `unbounded_operation` if neither.
└── No → proceed.

Is the result size disproportionate to evidence needed?
├── Yes → narrow the query. Emit `disproportionate_read`.
└── No → proceed.
```

### Evidence Interpretation Rules

| Tool result | Proves | Does NOT prove |
|-------------|--------|----------------|
| Test passes | Tested path works under tested conditions | Feature works in all cases; no regressions elsewhere |
| Test fails | Tested path is broken | The fix is wrong (test may be wrong); the feature is impossible |
| grep returns matches | Pattern exists in searched scope | Pattern is the only relevant one; pattern exists in unsearched scopes |
| grep returns nothing | Pattern not found in searched scope | Pattern doesn't exist anywhere; pattern doesn't exist under different naming |
| Command exits 0 | Command ran to completion | Command did what was intended; output is correct |
| Command exits non-zero | This invocation failed | The goal is impossible; no valid invocation exists |
| File read succeeds | File contents as of read time | File hasn't changed since read; contents are current |
| Build succeeds | Code compiles/builds | Code is correct; code is complete; no runtime errors |
| Lint passes | No lint-rule violations | Code is well-architected; code is secure; code is performant |
| Type check passes | Types are consistent | Logic is correct; all edge cases handled |

Overclaim check:
- Treating "test passes" as "feature works" → emit `overclaimed_evidence`
- Treating "grep empty" as "doesn't exist" → emit `absence_inference`
- Treating "exit 0" as "correct" → emit `exit_code_misread`
- Treating "build succeeds" as "code is correct" → emit `overclaimed_evidence`

### Termination Procedure

```
Tool call failed.

Step 1: Classify the failure
├── Transient (network, timeout, lock) → retry once with same input
├── Input likely wrong → retry once with varied input
├── Approach likely wrong → do NOT retry. Go to Step 3.
└── Unclear → retry once with diagnostic variation

Step 2: First retry failed. Classify again.
├── Same error, same input → emit `retry_without_variation`. Do NOT retry again.
├── Different error → one more retry with new variation
└── Approach confirmed wrong → go to Step 3.

Step 3: Retry bound exceeded.
├── Emit `retry_bound_exceeded`
├── Escalate to ANCHOR Recovery Discipline
├── FUSE owns the finding (what failed, what was tried)
├── ANCHOR owns the recovery procedure (last verified state, new approach, cost)
└── One merged surface block, not two.

Hung call procedure:
├── Call running past useful bound → kill it (job_kill for background, or abandon)
├── Do not wait indefinitely
└── Emit `unbounded_operation` if no timeout was set
```

### Restraint Decision Tree

```
Why am I calling this tool?
├── To produce evidence I don't have → legitimate. Proceed.
├── To reconfirm what I already know → emit `unnecessary_tool_call`. Skip.
├── To appear busy/thorough → emit `performative_tool_call`. Skip.
└── To do what a built-in can do →
    Can edit/view/glob/grep do this natively?
    ├── Yes → switch. Emit `heavyweight_for_lightweight`.
    └── No → bash is justified. Proceed.

Read scope check:
├── Reading whole file when I need one function? → use offset/limit. Emit `heavyweight_for_lightweight`.
├── Reading 500 lines to find one line? → grep first, then view. Emit `heavyweight_for_lightweight`.
└── Reading a file I already read this session? → Skip unless it may have changed.

Search check:
├── Searching when I know the path? → just read it. Skip the search.
├── Searching the whole repo for one file? → narrow scope. Emit `disproportionate_read`.
└── Searching for something I already found? → Skip.
```

---

## Integration Specs

### FUSE → ANCHOR

FUSE signals trigger ANCHOR state transitions. The handoff mirrors OWL's handoff to ANCHOR.

| FUSE signal | ANCHOR response | Format |
|-------------|-----------------|--------|
| `retry_bound_exceeded` | Recovery Discipline trigger (Failed transition). FUSE owns the finding; ANCHOR owns the recovery procedure. One merged block. | See below. |
| `overclaimed_evidence` | Epistemic Classification trigger. Reclassify the claim from Verified to Inferred. | ANCHOR tracks the reclassification; no separate surface block. |
| `absence_inference` | Epistemic Classification trigger. Reclassify from Verified to Speculative. | ANCHOR tracks the reclassification; no separate surface block. |

**FUSE-ANCHOR failure handoff format** (mirrors OWL-ANCHOR):

```
**Termination:** [finding — what failed and how many retries]. [implication — what continuing costs.]
**[Recovery Discipline]:** Last verified: [state]. New approach: [approach]. Reset cost: [X].

[action]
```

If FUSE has not emitted `retry_bound_exceeded` (e.g., FUSE was not run), ANCHOR surfaces the full active format independently — same as the OWL-ANCHOR contract.

### FUSE → SISPIS

FUSE signals map to SISPIS entropy dimensions. When FUSE runs before SISPIS, emitted signals pre-score specific SISPIS entropy signals.

The SISPIS entropy signals are: `option_multiplicity`, `tradeoff_density`, `ambiguity_of_framing`, `comparative_intent`, `downstream_impact`.

#### Mapping Table

| FUSE signal_type | SISPIS signal affected | Delta |
|------------------|----------------------|-------|
| `overclaimed_evidence` | `tradeoff_density` | +1 |
| `absence_inference` | `ambiguity_of_framing` | +1 |
| `retry_bound_exceeded` | `option_multiplicity`, `tradeoff_density` | +1 each |
| `unsafe_parallelization` | `ambiguity_of_framing` | +1 |
| `tool_affordance_mismatch` | `downstream_impact` | +1 |
| `unverified_external_claim` | `ambiguity_of_framing` | +1 |
| `wrong_tool_for_evidence` | `downstream_impact` | +1 |
| `out_of_order_execution` | `downstream_impact` | +0.5 |
| `skipped_prerequisite` | `ambiguity_of_framing` | +0.5 |
| `unbounded_operation` | `downstream_impact` | +0.5 |
| `retry_without_variation` | `tradeoff_density` | +0.5 |
| `unnecessary_tool_call` | `tradeoff_density` | +0.5 |
| `false_serialization` | `downstream_impact` | +0.5 |
| `disproportionate_read` | `downstream_impact` | +0.5 |
| `exit_code_misread` | `ambiguity_of_framing` | +0.5 |
| `performative_tool_call` | `tradeoff_density` | +0.5 |
| `heavyweight_for_lightweight` | `downstream_impact` | +0.5 |

#### Capping

SISPIS entropy signals are scored 0-2 per signal. FUSE deltas can push a signal past 2. Cap each SISPIS signal at 2.0 after applying deltas. Overflow does not carry to adjacent signals.

#### Pipeline Operation

```
1. FUSE runs strategy pass → emits signals with weights
2. FUSE gate: if W_fuse >= 1.5, surface findings before action
3. FUSE executes the action(s)
4. FUSE runs Evidence Interpretation on results → may emit additional signals
5. FUSE passes emitted signal list to ANCHOR (state transitions) and SISPIS (entropy)
6. SISPIS applies delta table to its entropy score E
7. SISPIS runs its gate function on the resulting E
8. Output mode: NO_DECISION / EXPLANATION / SCHEMA
```

FUSE can elevate E enough to cross SISPIS thresholds. A request that SISPIS would ordinarily resolve as NO_DECISION may become SCHEMA after FUSE finds `retry_bound_exceeded` (+1 to two signals, potentially pushing E from 2 to 4).

### OWL → FUSE

OWL's signals define what FUSE needs to verify. The handoff:

| OWL signal | FUSE response |
|-----------|---------------|
| `code_not_read` | FUSE Sequencing: schedule read before edit |
| `contradiction` | FUSE Necessity: tool call required to resolve |
| `missing_criteria` | FUSE Evidence Interpretation: define what result would prove success |
| `unverifiable_claim` | FUSE Necessity: if no tool can verify, label as inferred |
| `partial_completion` | FUSE Evidence Interpretation: verify the completed portion, label the rest |

OWL does not tell FUSE which tool to use. OWL says "this needs verifying"; FUSE decides how.

### FUSE + WARD

WARD runs alongside FUSE at each action decision point. FUSE selects the action; WARD gates whether it is allowed. The handoff:

| FUSE action | WARD response | FUSE next step |
|-------------|---------------|-----------------|
| Action selected (tool + strategy) | `proceed` | Execute normally |
| Action selected | `constrain` | Re-run Selection + Sequencing with WARD's constraints applied |
| Action selected | `confirm` | Halt. Wait for user confirmation before executing |
| Action selected | `refuse` | Do not execute. Escalate to ANCHOR Recovery Discipline. FUSE does not retry. |
| Action selected | `recover` | Halt. ANCHOR state recovery required before any further execution. |

For pure reads of project files with no boundary crossing, no secret exposure, and no policy implication, WARD does not fire — FUSE executes without WARD gating.

---

## Integration Test Cases

### Low-signal action (W_fuse = 0)
Read a file at a known path. Single call, unambiguous target, only fitting tool, no retry/parallel/evidence ambiguity. Suppression condition applies. W_fuse = 0. Silent. Call proceeds.

### Single weight-1.0 signal (W_fuse = 1.0)
`overclaimed_evidence` fires — a green test is being treated as "feature works." W_fuse = 1.0 < 1.5 → silent. SISPIS receives +1 to `tradeoff_density`. ANCHOR receives Epistemic Classification trigger (reclassify to Inferred). The claim is adjusted internally; no surface block.

### Two weight-1.0 signals (W_fuse = 2.0)
`retry_bound_exceeded` + `overclaimed_evidence`. Surfaces. SISPIS receives +1 to `option_multiplicity`, +1 to `tradeoff_density` (from retry), +1 to `tradeoff_density` (from overclaim, capped at 2). ANCHOR receives Recovery Discipline trigger. E elevates; SISPIS likely activates SCHEMA mode.

### Single weight-1.0 + weight-0.5 (W_fuse = 1.5)
`unsafe_parallelization` + `false_serialization`. Surfaces (1.5 >= 1.5). SISPIS receives +1 to `ambiguity_of_framing` and +0.5 to `downstream_impact`. The batch is restructured before execution.

### Evidence-only signal post-execution
After running a test that passes, Evidence Interpretation checks the claim being made. If the claim is "the tested path works" — no signal, the evidence matches. If the claim is "the feature is bug-free" — `overclaimed_evidence` fires. FUSE's post-execution pass can surface even when the pre-execution pass was silent.
