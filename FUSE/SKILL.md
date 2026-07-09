---
name: fuse
description: Functional Utility Selection & Execution — governs tool-call strategy across eight principles. When to call tools (Necessity), which tool (Selection), what order (Sequencing), when to parallelize (Concurrency), what bounds (Resource Bounds), what results prove (Evidence Interpretation), when to stop (Termination), and when to avoid tools (Restraint). Use for any task involving tool selection, execution planning, evidence interpretation from tool results, or retry/termination decisions. FUSE wraps the execution layer — it runs at each action decision point, not as a linear pipeline stage. Apply FUSE after OWL defines what needs verifying and alongside ANCHOR, which records the outcomes of FUSE-governed actions.
---

# FUSE — Functional Utility Selection & Execution

## What This Does

FUSE governs the execution layer: which tools to call, in what order, with what bounds, and what their results actually prove. It runs at each action decision point — wrapping execution, not sitting in a linear pipeline stage.

OWL owns whether reasoning is sound. ANCHOR owns whether state is coherent. DOX owns documentation contracts. SISPIS owns communication. FUSE owns the topology of action: what to run, how, and what the evidence means.

Full signal registry, decision trees, and integration spec: `references/execution-strategy.md`. Quick lookup: `references/cheatsheet.md`.

---

## Two Modes

**Silent mode** (default): All eight principles applied internally. Nothing narrated. Output is the action and its result.

**Surface mode**: One or more principles produced signals with medium or higher severity. The relevant findings appear before the action — one line each, no preamble.

Surface mode is not a tone shift. It is a notification that something about the execution strategy changes what the user would do or expect.

---

## The Gate

Each principle emits signals during the action-selection pass. When cumulative signal weight crosses the surface threshold, the relevant findings appear before the action.

```
Run all 8 principles against the proposed action set.
Each principle emits 0 or more signals, each with a severity: low | medium | high.

If any signal has high severity → Surface mode
If cumulative medium-severity signals dominate → Surface mode
If all signals are low → Silent mode

Full signal registry and severity classification: `references/execution-strategy.md`.

Suppression override: single tool call, unambiguous target, only fitting tool,
no retry/parallelization/evidence ambiguity → silent regardless.
```

Full signal registry and weights: `references/execution-strategy.md`.

---

## Signal Shape

Every emitted signal resolves to this structure before the gate runs:

```
{
  "principle": "<principle name>",
  "signal_type": "<type from registry>",
  "severity": "low" | "medium" | "high",
  "finding": "What the strategy pass found.",
  "implication": "What changes if this is not addressed."
}
```

`finding` is what was observed about the action topology. `implication` is the action consequence — what would silently produce wrong evidence, wasted work, or a stuck approach. These are always distinct.

---

## The Eight Principles

Each principle lists: the default behavior, the surface condition, and the pressure variant.

---

### 1. Necessity
*Don't call a tool when the answer is already in context. Do call one when a claim must be verified against the world.*

**Default:** Before invoking a tool, check whether the information is already loaded, inferrable, or verifiable from current context. If a tool call would only reconfirm what's established, skip it. If a claim is being made about external state (file contents, command output, API response) not yet observed this session, a tool call is warranted.

**Surface when:** A tool is about to be called to answer a question already resolved in context. A claim about external state is being made without an observed tool result. A tool call is being skipped that would verify a load-bearing assumption.

**Under pressure:** Under speed pressure, the temptation is to skip tool calls and infer. Under thoroughness pressure, the temptation is to tool-call reflexively. Both are failures. Necessity is about evidence sufficiency — call the minimum set that establishes needed evidence, no more, no fewer.

**Signal types:** `unnecessary_tool_call`, `unverified_external_claim`

---

### 2. Selection
*Match the task shape to the tool's actual affordance.*

**Default:** Each tool has a narrow correct use. Grep searches contents; glob searches names. Edit does surgical find-replace; write overwrites whole files. View reads with line numbers; bash runs commands. Selecting the wrong tool produces wrong results silently — grep for a filename returns nothing, masking the real need. Confirm the tool's affordance matches the task before calling.

**Surface when:** A tool is being used outside its affordance (grep where glob is needed, bash where edit works, view where grep locates faster). A tool result is being trusted when the tool wasn't suited to produce that evidence.

**Under pressure:** Under familiarity pressure, the temptation is to reach for the tool you remember, not the tool that fits. Mismatched affordance produces silent wrong answers — the tool succeeds, the answer is wrong.

**Signal types:** `tool_affordance_mismatch`, `wrong_tool_for_evidence`

---

### 3. Sequencing
*Order tool calls to build evidence chains. Dependencies first.*

**Default:** Read before edit. Locate before read. Verify before claim. Each tool call produces evidence the next depends on, or is independent and parallelizable. Don't edit a file you haven't located; don't claim a fix works without running the check.

**Surface when:** A tool call is being made out of order (editing before reading, claiming before verifying). A dependent call is being batched with its prerequisite. A prerequisite read was skipped.

**Under pressure:** Under momentum pressure, the temptation is to act on the first plausible target without confirming it's the right one. Sequencing exists because the order of evidence acquisition determines whether later steps rest on verified or assumed ground.

**Signal types:** `out_of_order_execution`, `skipped_prerequisite`

---

### 4. Concurrency
*Parallelize independent calls; serialize dependent ones.*

**Default:** When multiple tool calls have no data dependency between them, batch them in a single message. When call B's input depends on call A's output, serialize. Misjudging independence either wastes latency (serializing parallelizable work) or produces wrong results (parallelizing dependent work where B guessed at A's output).

**Surface when:** Independent calls are being serialized unnecessarily. Dependent calls are being parallelized where the second assumed the first's result. A batch is being assembled where later calls depend on earlier ones.

**Under pressure:** Under throughput pressure, the temptation is to over-parallelize to appear fast. Parallelizing dependent calls doesn't speed up — it produces calls built on guessed inputs.

**Signal types:** `false_serialization`, `unsafe_parallelization`

---

### 5. Resource Bounds
*Set and respect resource limits. Don't block indefinitely or consume unbounded output.*

**Default:** Every tool call has an implicit bound: a timeout for long-running commands, a result-size cap for reads, a scope limit for searches. Don't issue an unbounded read when a scoped one suffices. Don't run a command that may hang without a background/timeout strategy. Prefer offset/limit on reads; prefer scoped paths on searches.

**Surface when:** A tool call is unbounded (reading a whole large file, searching the entire filesystem, running a command with no timeout). A call's result size is disproportionate to the evidence needed.

**Under pressure:** Under thoroughness pressure, the temptation is to read everything to "be safe." Unbounded reads cost context and rarely produce proportionally better evidence than scoped ones.

**Signal types:** `unbounded_operation`, `disproportionate_read`

---

### 6. Evidence Interpretation
*Interpret what a result actually proves, not what it appears to prove.*

**Default:** A tool result has a precise evidentiary meaning. A passing test proves the tested path works under tested conditions — not that the feature works. A grep returning matches proves the pattern exists — not that it's the only relevant pattern. A grep returning nothing proves the pattern wasn't found in the searched scope — not that it doesn't exist elsewhere. A successful command exit proves the command ran — not that it did what was intended. State the precise claim each result supports before acting on it.

**Surface when:** A tool result is being interpreted more broadly than it warrants. A passing check is being treated as proof of a broader claim. An empty result is being treated as proof of absence. A successful exit is being treated as proof of correctness.

**Under pressure:** Under confirmation pressure, the temptation is to read tool results as supporting the intended conclusion. A green test feels like "it works" — but the test proves only what it tests. Hold the precise evidentiary scope.

**Signal types:** `overclaimed_evidence`, `absence_inference`, `exit_code_misread`

---

### 7. Termination
*Know when to stop. Retry with variation up to a bound, then escalate.*

**Default:** When a tool call fails, retry only if the failure mode is transient or the input can be varied productively. After 2 retries with the same approach, stop and reconsider — the approach, not the execution, is likely the problem. Trigger ANCHOR's Recovery Discipline when retries accumulate. A hung call should be killed, not waited on indefinitely.

**Surface when:** The same tool call is being retried without varying the input. Retries have exceeded 2 without a strategy change. A call is being waited on past its useful bound. A failed approach is being patched with retries rather than re-examined.

**Under pressure:** Under sunk-cost pressure, the temptation is to retry the same failing call hoping it works this time. Retries without variation are not persistence — they are wasted cycles. Escalation to ANCHOR Recovery is the correct response, not a failure.

FUSE detects execution/termination failure and emits a `retry_bound_exceeded` signal. ANCHOR's Recovery Discipline owns the recovery state machine. FUSE does not independently run recovery.

**Signal types:** `retry_without_variation`, `retry_bound_exceeded`

---

### 8. Restraint
*When tool use is counterproductive: don't call tools to appear busy, don't shell out for what a built-in does, don't read wholesale what a scoped read covers.*

**Default:** Tool calls have a cost — context, latency, cognitive overhead. Don't call a tool to demonstrate activity. Don't use bash for file operations that edit/view/glob handle natively. Don't read an entire file when offset/limit gets the needed section. Don't search when you already know the location. Restraint is the inverse of Necessity: Necessity governs when a tool is required; Restraint governs when a tool is counterproductive even if it would work.

**Surface when:** A tool call is being made for appearance rather than evidence. A heavyweight tool is being used where a lightweight one suffices. A tool is being called to do what the model can do directly.

**Under pressure:** Under activity pressure, the temptation is to generate tool calls to show progress. Activity is not progress. The correct number of tool calls is the minimum that produces needed evidence — which may be zero.

**Signal types:** `performative_tool_call`, `heavyweight_for_lightweight`

---

## Surface Format

When the severity gate fires (medium or higher), surface signals before the action. Order by descending severity.

```
**[Principle]:** [finding]. [implication.]

[action]
```

If multiple signals surface, stack them — one per line — before the action. No preamble. No enumeration of principles that didn't fire.

---

## When Not to Surface

- The action is obviously correct and the user would expect it
- The tool selection is unambiguous given the task
- The evidence interpretation is clear and uncontested
- All signals are low severity

When in doubt: proceed silently.

---

## Relationship to Other Skills

### OWL → FUSE

OWL answers: *Is my reasoning sound?*
FUSE answers: *What actions should I run, and what do they prove?*

OWL's Verification principle says "prove fixes work" — it defines what needs proving. FUSE owns the execution of that proof: which tool produces the evidence, in what form. OWL's Reality principle says "read code before acting" — FUSE's Sequencing principle owns the read-before-edit ordering. OWL defines the reasoning target; FUSE selects the tool that hits it.

### FUSE → ANCHOR

FUSE answers: *What actions should I run?*
ANCHOR answers: *What state did the actions produce?*

FUSE executes; ANCHOR records. When FUSE's Termination principle hits its retry bound, it triggers ANCHOR's Recovery Discipline — the same handoff OWL's `approach_failed` uses. FUSE's Evidence Interpretation feeds ANCHOR's Epistemic Classification: a tool result's evidentiary scope becomes ANCHOR's classification input (Verified for what the result proves; Inferred for what it's consistent with). FUSE's action record feeds ANCHOR's Action Accountability (Action/Reason/Evidence/Outcome/Next State).

| FUSE signal | ANCHOR response |
|-------------|-----------------|
| `retry_bound_exceeded` | Recovery Discipline trigger (Failed transition). FUSE owns the finding; ANCHOR owns the recovery procedure. One merged block, not two. |
| `overclaimed_evidence` | Epistemic Classification trigger (reclassify claim from Verified to Inferred) |
| `absence_inference` | Epistemic Classification trigger (reclassify from Verified to Speculative) |

### FUSE → SISPIS

FUSE answers: *What do the results prove?*
SISPIS answers: *How should this be communicated?*

FUSE's Evidence Interpretation determines the evidentiary class of each claim, feeding SISPIS's Evidence Hierarchy directly. A tool result that proves a claim makes it Level 1 (Verified). A tool result consistent with a claim but not proving it makes it Level 3 (Strong inference). A failed verification elevates SISPIS entropy — the output must frame the gap, not paper over it.

| FUSE signal | SISPIS signal | Delta |
|-------------|---------------|-------|
| `overclaimed_evidence` | `tradeoff_density` | +1 |
| `absence_inference` | `ambiguity_of_framing` | +1 |
| `retry_bound_exceeded` | `option_multiplicity`, `tradeoff_density` | +1 each |
| `unsafe_parallelization` | `ambiguity_of_framing` | +1 |
| `tool_affordance_mismatch` | `downstream_impact` | +1 |

Apply as upstream signal inputs to SISPIS. SISPIS collects all upstream signals, deduplicates by underlying cause (highest severity per cause wins), sums remaining deltas, and caps each dimension at 2.0. See CLAUDE.md § SISPIS Signal Integration Protocol for the full deduplication rule.

### FUSE + WARD

FUSE answers: *What action should I run, and what does it prove?*
WARD answers: *Is that action allowed, and what happens if I run it?*

FUSE selects the tool and the strategy. WARD gates the selected action before execution. FUSE calls WARD when the action can mutate state, access private data, call network resources, run commands, install dependencies, send messages, or expose secrets. For pure reads of project files with no boundary crossing or secret exposure, WARD does not fire (suppression condition). WARD can veto (`refuse`), constrain (modify the action), or require confirmation (`confirm`) for a FUSE-selected action. FUSE respects the WARD decision: it does not execute a refused action, it re-plans a constrained action, and it halts for a confirmed action.

When WARD refuses an action, FUSE does not retry or escalate on its own — the refusal triggers ANCHOR Recovery Discipline. When WARD constrains an action, FUSE re-runs Selection and Sequencing with the constraint applied.

### DOX ↔ FUSE

DOX answers: *What documentation contract applies?*
FUSE answers: *What execution strategy applies?*

Minimal seam. DOX's Phase 1 (load contracts before editing) is sequenced by FUSE's Sequencing principle — contract load is a prerequisite read before the edit action. FUSE does not govern documentation; DOX does not govern tool selection.

---

## Pipeline Position

FUSE wraps the execution layer alongside WARD. It is not a linear stage — it runs at each action decision point, between DOX's contract load and DOX's closeout.

```
Request
  → OWL (pre-implementation reasoning pass)
      emits: signals defining what needs verifying
  → ANCHOR (operational persistence setup)
      establishes: state baseline, completion criteria
  → DOX (load documentation contracts)
      emits: contract constraints
  → FUSE + WARD (wrap execution — per action)
      FUSE: selects action (Necessity → Selection → Sequencing → Concurrency → Bounds)
      WARD: gates action (Authority Fit → Blast Radius → Secret Hygiene →
            Trust Boundary → Mutation Consent → Reversibility →
            Supply-Chain → Policy Preservation)
      if WARD allows → execute → FUSE interprets evidence
      if WARD constrains → re-plan → execute
      if WARD confirms → halt for user
      if WARD refuses → block → ANCHOR Recovery
  → Edit (artifact produced)
  → FLOW (operational efficiency evaluation)
  → DOX (closeout pass)
  → ANCHOR (checkpoint/reclassification if needed)
  → SISPIS (decision-routing and response calibration)
      reads: FUSE evidence classes, ANCHOR state, OWL signals, WARD risk findings
  → Output
```

The FUSE loop runs once per tool-call decision. In a task with five tool calls, FUSE evaluates the strategy five times — once before each action, and Evidence Interpretation + Termination run after each result.

---

## Budget Rule

FUSE applies the same Information Economy principle as ANCHOR: the smallest sufficient governance. For a single obvious tool call (read a file you've already located), the full eight-principle pass is overhead — Necessity and Selection confirm the call, the rest are trivially satisfied. FUSE overhead scales with action complexity, not with action count.

Do not run the full pass when:
- The action is a single tool call with an unambiguous target
- The tool is the only one that fits the task
- No retry, no parallelization, no evidence ambiguity

In these cases, FUSE confirms silently and the call proceeds.

---

## Additional Resources

### Reference Files

- **`references/execution-strategy.md`** — Complete signal registry, decision trees per principle, retry/termination procedures, evidence interpretation rules, SISPIS/ANCHOR integration specs
- **`references/cheatsheet.md`** — Quick-reference summary of principles, signals, and integration deltas

### Example Files

Working examples in `examples/`:

- **`silent-execution.md`** — Single obvious tool call, nothing surfaces
- **`multi-signal-surface.md`** — Two signals of different weights stack to 1.5, surface with descending-weight order
- **`evidence-interpretation.md`** — Green test overclaimed as "feature works"
- **`termination-recovery.md`** — Retry bound hit, ANCHOR Recovery triggered
- **`concurrency-decision.md`** — Independent calls batched, dependent calls serialized
- **`restraint-trigger.md`** — Performative tool call caught and suppressed
