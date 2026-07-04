# CognitiveFrameWorks

A set of behavioral protocols for AI assistants. Seven skills: OWL, ANCHOR, DOX, FUSE, FLOW, WARD, and SISPIS. Designed to run as a pipeline, but each functions independently.

---

## Why This Matters

AI coding assistants fail in predictable ways: they hallucinate fixes without reading the code, expand scope beyond what was asked, run unsafe commands, claim a passing test proves the feature works, and lose track of what they've already tried after a long session. These are not random errors — they are systematic patterns that compound under pressure.

CognitiveFrameWorks targets five failure classes directly:

- **Hallucinated fixes** → OWL's Reality principle forces code-reading before implementation; Epistemics separates verified facts from assumptions
- **Scope creep** → OWL's Locality principle constrains changes to what the request implies; surfaces scope expansion before it happens
- **Unsafe tool use** → WARD gates destructive commands, secret exposure, trust-boundary crossings, and authority violations before execution
- **Verification overclaiming** → FUSE's Evidence Interpretation principle defines exactly what a tool result proves vs. what it appears to prove
- **Context drift** → ANCHOR's Memory Integrity checkpoints state; Recovery Discipline forces resets instead of patch accumulation

Each skill catches a different class of error. Together, they form a pipeline where reasoning failures are caught before implementation, execution failures are caught before they compound, and communication failures are caught before they mislead.

---

## Quickstart

### Use an adapter with your tool

Each skill ships platform-ready adapters. Copy the adapter for your tool into your project:

| Platform | File | Destination |
|----------|------|-------------|
| Claude / Cursor rules | `OWL/adapters/CLAUDE.md` | `.claude/CLAUDE.md` or project root |
| Cursor | `OWL/adapters/.cursorrules` | Project root |
| Windsurf | `OWL/adapters/.windsurfrules` | Project root |
| Aider | `OWL/adapters/.aider.conf.yml` | `~/.aider.conf.yml` or project root |
| Continue | `OWL/adapters/continue-config.yaml` | VS Code Continue config |
| System prompt (any) | `OWL/adapters/system-prompt.md` | Paste into your model's system prompt |

Repeat for each skill you want active. Start with OWL alone — it's the highest-value single skill.

### Minimal active subsets

You don't need all seven skills. Load only what the task needs:

| Task | Skills |
|------|--------|
| Quick question or explanation | SISPIS only (or OWL + SISPIS if ambiguity exists) |
| Single-turn code advice | OWL + SISPIS |
| Multi-turn debugging | OWL + ANCHOR + FUSE + SISPIS |
| Tool-using implementation | OWL + ANCHOR + FUSE + WARD + SISPIS |
| Performance review | OWL + FLOW + SISPIS |
| File editing in a DOX-enabled project | OWL + ANCHOR + DOX + FUSE + WARD + SISPIS; FLOW if triggered |
| Risky commands / secrets | WARD required |

---

## Architecture

```
Request
  │
  ▼
OWL ─────── pre-implementation reasoning (9 principles)
  │           emits signals with severity
  ▼
ANCHOR ──── state baseline (8 principles)
  │           checkpoints, object identity, epistemic classification
  ▼
DOX load ── contract hierarchy (only in DOX-enabled projects)
  │           reads AGENTS.md chain before editing
  ▼
FUSE+WARD ─ wrap every tool/action decision
  │  │        FUSE: which tool, what order, what result proves
  │  │        WARD: is the action allowed, safe, reversible?
  │  ▼
  │  Edit ── artifact produced
  │
  ▼
FLOW ────── operational drag evaluation (only if trigger fires)
  │           retries, backpressure, caches, hot paths, N+1 queries
  ▼
DOX closeout (only if durable contracts changed)
  │
  ▼
ANCHOR ──── checkpoint / reclassify if needed
  │
  ▼
SISPIS ──── output calibration
  │           direct answer vs. explanation vs. decision framework
  ▼
Output
```

FUSE + WARD run per-action, not per-request. FLOW runs once per artifact, only if a trigger area is touched. DOX load runs before edits; DOX closeout runs after FLOW.

---

## Validation

```bash
python3 scripts/validate-framework.py
```

Checks JSON syntax, skill count, adapter generation drift, WARD `recover` mapping, canonical integration drift, and adapter duplicate hashes.

---

## Examples

### Bugfix — OWL catches a contradiction before code is written

> **User:** Fix the login bug — the API returns 500 on POST /auth/login.
>
> **OWL (Reality):** The existing handler at `src/auth/login.py:42` already wraps the password check in a try/except. The 500 is thrown by `verify_token()` at line 58, which is called *after* successful login — not during. Fixing the password check won't address the 500.
>
> **OWL (Epistemics):** The request assumes the bug is in password verification. The traceback points to token verification. These are different code paths.
>
> Fix targets `verify_token()` — the error is a missing null check on the refresh token, not a password verification issue.

Without OWL, the model reads "login bug" and patches the password path. OWL's Reality principle forced reading the actual code first, and its Epistemics principle separated the user's framing from what the code shows.

---

### Dangerous command blocked by WARD

> **User:** Clean up the old config files in /var/lib/myapp.
>
> **WARD (Authority Fit):** The user said "clean up old config files" — this grants authority to remove specific files in `/var/lib/myapp/`, not to recursively delete the entire data directory.
>
> **WARD (Blast Radius):** The proposed command `rm -rf /var/lib/myapp` would delete the application data directory irreversibly. No backup exists. Blast radius: high — irreversible data loss across the application.
>
> **Required action: refuse.**
>
> Action blocked. The scoped equivalent is `rm /var/lib/myapp/*.old.conf` — removing only files matching the `.old.conf` pattern.

WARD refused the broad command and proposed the narrowest safe alternative. Without WARD, the model matches "clean up" to `rm -rf` and destroys the data directory.

---

### Messy debugging session recovered by ANCHOR

> **Turn 1–3:** Model investigates a flaky test. Tries mocking the database (approach A). Test still flaky.
> **Turn 4–6:** Adds a retry wrapper around the test assertion (approach B). Test passes twice, then fails again.
> **Turn 7:** Model is about to add a third patch on top of B.
>
> **ANCHOR (Recovery Discipline):** Current approach has failed twice (same error: assertion timeout despite retry). Transitioning Degraded → Failed.
>
> **[Recovery]:** Last verified state: test passes reliably when run against the real database (turn 2). Approaches A (mock DB) and B (retry wrapper) both addressed symptoms, not the cause. New approach: investigate why the mock diverges from the real database — the flake is likely a mock fidelity issue, not a timing issue. Reset cost: 2 turns of investigation.

ANCHOR stopped the patch accumulation at 2 attempts, identified the last known-good state, and redirected to a structurally different approach. Without ANCHOR, the model continues stacking patches on a failed approach.

---

## ANCHOR

### Why It Exists

Most agent failures are not reasoning failures. They are persistence failures. The model forgets prior conclusions, merges unrelated issues, loses verification status, continues failed approaches, cannot reconstruct state after context compression, and cannot explain why a change occurred. Reasoning remains sound locally. The operational state becomes corrupted globally.

ANCHOR addresses this by preserving execution continuity. It sits between OWL and DOX in the pipeline — after reasoning produces signals, before documentation contracts are loaded. Its domain is not reasoning quality (OWL), documentation contracts (DOX), or communication calibration (SISPIS). It is the coherence of operational state across the session.

OWL answers: *Is my reasoning sound?*
ANCHOR answers: *Is my execution state coherent?*
DOX answers: *What documentation contract applies?*
FUSE answers: *What execution strategy is justified, and what does the evidence prove?*
FLOW answers: *Does the produced artifact create operational drag?*
SISPIS answers: *How should this be communicated?*
WARD answers: *Is this action allowed, and what happens if I run it?*

ANCHOR's eight principles: State Integrity (separate facts from assumptions), Object Continuity (stable identities across renames and partial fixes), Memory Integrity (checkpoint and reconstruct), Epistemic Classification (every claim is Verified/Observed/Inferred/Speculative/Unknown), Recovery Discipline (Active → Degraded → Failed → Recovered, no patch accumulation), Completion Discipline (done = criteria met, not activity stopped), Action Accountability (traceable state transitions), and Information Economy (smallest sufficient change — including in ANCHOR itself).

Like OWL, ANCHOR runs silently by default. It surfaces only when a principle requires explicit state maintenance — a checkpoint must be written, a recovery must be initiated, an object identity must be preserved, or completion criteria must be stated.

**Checkpoints are graduated by scope.** A micro-checkpoint [MC-N] preserves 1–3 items in a single line for short sessions. A standard checkpoint [CP-N] uses the full sectioned format for sessions with significant accumulated state. A full checkpoint [FCP-N] adds reconstruction notes for handoffs or imminent compression. The smallest format that preserves what matters is always preferred.

**Object tracking includes retirement.** Objects accumulate in the registry as sessions grow. A Resolved or Superseded object is archived after it has been cited once in a post-resolution decision. Archived objects retain identity and lineage but don't count toward active tracking overhead.

**Epistemic state is shared with OWL.** OWL's pre-implementation signals establish the starting epistemic classification for the session. ANCHOR inherits that baseline and tracks reclassifications as new evidence arrives. It does not re-classify what OWL has already surfaced.

**Recovery output merges with OWL.** When OWL's Integrity principle has already surfaced an `approach_failed` signal, ANCHOR does not produce a separate recovery block. OWL owns the finding and implication; ANCHOR extends it with the recovery procedure — last verified state, new approach, cost. One block, not two.

---

## SISPIS

### Why It Exists

AI models operate at a level of technical fluency that assumes the person on the other end is operating at the same level. That assumption is often wrong. A model explaining a merge conflict, a deployment decision, or an architectural tradeoff will default to the framing that's most natural to it — which is expert-to-expert framing — regardless of whether the person reading it has the context to act on it.

The problem isn't that models are too technical. The problem is that they don't calibrate. A model that explains a caching strategy the same way to a senior infrastructure engineer and to a product manager who just needs to know whether to proceed isn't being thorough — it's failing both of them, in different ways.

At the same time, the solution isn't to simplify everything. Models interact with code, with APIs, with systems — contexts where precision matters and dumbing down introduces its own errors. And models interact with highly technical users who need and expect expert-level depth. Flattening communication across all contexts would be the wrong correction.

The further wrinkle: in high-velocity, rapidly iterative development flows, this mismatch doesn't announce itself. It accumulates. A decision made on a response that was technically correct but framed at the wrong altitude. A tradeoff not surfaced because it was assumed to be obvious. A recommendation followed without understanding why. Over time this becomes a kind of unspoken technical debt — not in the code, but in the decisions made around it. It's rarely attributed to communication calibration failures because those failures don't look like failures in the moment. They look like normal interaction.

SISPIS addresses this as an output calibration gate. It does not perform decision analysis — OWL and ANCHOR do that. What SISPIS does is determine how to communicate their results. Simple or purely informational queries get direct answers. Queries with real decision structure — tradeoffs, options, downstream consequences — get a structured framework that makes the decision visible. The gate uses a deterministic scoring rubric rather than freeform model preference, so it fires consistently rather than based on what a model feels like doing in a given moment. The schema it activates is designed to surface what a person actually needs to make a call: what triggered this, what it means practically, what the options are, what they cost, and what the recommended path is.

SISPIS runs last in the pipeline, after all upstream skills have completed their passes. It reads signals from OWL, ANCHOR, FUSE, FLOW, and WARD through the canonical mappings in `shared/integration.md`. The combined signal determines the output mode.

SISPIS also preserves evidence altitude in final communication: Verified claims, Observed behavior, Inferred conclusions, and Speculative recommendations are not flattened into the same confidence level.

The goal: maximum analytical depth internally, calibrated communication externally. Not simpler — clearer.

---

## OWL

### Why It Exists

Andrej Karpathy published a set of observations about common LLM coding mistakes — a short list of behavioral guidelines that, if followed, would catch most of the patterns that cause AI-generated code to be subtly wrong, over-engineered, or hard to debug. The original Karpathy skills formalize those observations into an always-on set of principles: think before coding, stay simple, make surgical changes, define success criteria.

OWL started from that foundation and extended it in two directions.

The first extension is scope. The original principles cover the most common failure modes well, but they leave gaps. Conservation — preserving the behavioral intent of existing code, not just its structure — is distinct from Locality, which constrains scope. Debuggability is distinct from Simplicity. Integrity — the principle that governs what to do when an approach has failed, and whether to reset or keep patching — has no analog in the original set. OWL expands the taxonomy to nine principles with explicitly named signal types per principle, and a threshold-based gate that determines when to surface a finding versus proceed silently.

The second extension is pressure behavior. Karpathy's principles describe how to behave. They don't describe how to hold that behavior when conditions make it hard — a long task where context has drifted, a user who disagrees with a correct diagnosis, an approach that has failed twice but represents significant invested effort. OWL adds a pressure variant to each principle: the same principle, specified for the conditions under which models most commonly violate it. This isn't a separate layer — it's each principle extended to its harder case.

OWL also produces structured output — a signal shape with a finding, an implication, a principle, a type, and a weight — rather than prose. This is the integration point with both ANCHOR and SISPIS. OWL's signals map to SISPIS's entropy scoring through the canonical mappings in `shared/integration.md`, allowing code-reasoning findings to elevate SISPIS's gate before it evaluates response structure. They also trigger ANCHOR state transitions: `approach_failed` triggers Recovery Discipline, `constraint_drift` triggers Memory Integrity checkpointing, `partial_completion` triggers Completion Discipline. A request that SISPIS would resolve as a simple direct answer can become a structured decision framework after OWL finds a contradiction. A recovery that OWL and ANCHOR both handle produces one merged surface block, not two.

---

## DOX

### Why It Exists

Most AI skills assume the codebase is well-documented or that documentation can be figured out from context. In practice, projects accumulate AGENTS.md files, READMEs, and inline docs at different times by different people, with no protocol for keeping them current. The docs drift. The agent works from stale contracts. The user gets output that reflects what the docs said six months ago, not what the code does now.

DOX addresses this by treating AGENTS.md files as a binding work contract hierarchy. Every subtree owns its documentation. Before editing, the agent walks the DOX chain from root to target and reads every applicable contract. After editing, it runs a closeout pass: update the nearest owning docs, refresh child indexes, remove stale content.

When Phase 1 loads a contract and finds a constraint that applies to the current edit scope, DOX emits a contract signal before the edit proceeds. This is a defined surface format — `[DOX contract]: [constraint]. [Applies to: scope.]` — readable by OWL and ANCHOR. OWL's Locality and Conservation principles absorb it as scope and behavior constraints. ANCHOR's State Integrity absorbs it as a verified constraint. DOX does not score entropy or produce signal weights; it surfaces the constraint once, before the edit, and lets the upstream skills act on it.

The key constraint on DOX itself: it preserves contracts, it doesn't expand scope. It doesn't opportunistically fix unrelated docs. It doesn't scan the whole repo unless explicitly asked. It loads the minimum chain needed for the current task, and after the edit, it updates only the docs the change actually affects.

---

## FUSE

### Why It Exists

OWL can say "this needs verifying" — it does not own which tool verifies it, in what order, or what the result actually proves. ANCHOR tracks state after actions — it does not decide the action topology. DOX governs doc contracts. SISPIS governs communication. None of them own the execution layer: which tool to call, when to parallelize, when to retry, when to stop, and what evidence a result actually produces versus what it appears to produce.

The gap matters because tool-use decisions are the most frequent decision an agent makes. Every turn where a tool is called involves selection, sequencing, evidence interpretation, and termination decisions. These are currently ungoverned — made ad hoc, inconsistently, and most likely to fail exactly when pressure is highest (retrying the same failing call, over-reading to "be safe," overclaiming what a green test proves).

FUSE addresses this with eight principles: Necessity (when to call tools at all), Selection (which tool matches the task's affordance), Sequencing (dependencies first — read before edit, verify before claim), Concurrency (parallelize independent calls, serialize dependent ones), Resource Bounds (timeout, scope, result-size limits), Evidence Interpretation (what a result proves vs. what it appears to prove — a green test proves the tested path, not the feature), Termination (retry with variation, max 2, then escalate to ANCHOR's Recovery Discipline), and Restraint (when tool use is counterproductive — don't call to appear busy, don't shell out for built-ins, don't read wholesale what scoped reads cover).

Like the others, FUSE runs silently by default. It surfaces only when the execution strategy itself would change what the user does or expects — an unsafe parallelization, an overclaimed evidence interpretation, a retry bound exceeded. FUSE wraps the execution layer rather than sitting in a linear pipeline stage. It runs at each action decision point: before each tool call (Necessity, Selection, Sequencing, Concurrency, Resource Bounds) and after each result (Evidence Interpretation, Termination).

FUSE integrates with the other skills through the canonical mappings in `shared/integration.md`. OWL's Verification principle defines what needs proving; FUSE owns the execution of that proof. FUSE's Termination principle escalates to ANCHOR's Recovery Discipline when retries are exhausted — the same handoff OWL uses for `approach_failed`. FUSE's Evidence Interpretation feeds both ANCHOR's Epistemic Classification (reclassifying claims based on what evidence actually proves) and SISPIS's Evidence Hierarchy (determining whether a claim is Verified, Observed, or Inferred). WARD runs alongside FUSE at each action decision point — FUSE selects the action, WARD gates whether it is allowed. When WARD refuses an action, FUSE does not retry — the refusal triggers ANCHOR Recovery.

---

## WARD

### Why It Exists

FUSE decides which tool to call, in what order, and what the result proves — it does not gate whether the action is allowed. OWL evaluates reasoning. ANCHOR tracks state. FLOW evaluates operational drag. None of them own authority, trust boundaries, secrets, destructive actions, or blast-radius control.

This gap matters because the most consequential agent failures are not reasoning failures, tool-selection failures, or efficiency failures — they are authority failures. The agent runs a broader command than the user intended. It reads secrets into context that surface in output. It crosses a trust boundary without acknowledgment. It executes untrusted code. It widens permissions to make progress. These are not correctness bugs — they are governance failures.

WARD addresses this with eight principles: Authority Fit (action stays within what the user granted), Blast Radius (prefer smallest reversible action; name destructive/irreversible effects), Secret Hygiene (detect, avoid exposing, avoid logging, avoid transmitting credentials/secrets), Trust Boundary Discipline (distinguish local/project/user/private/external before crossing), Mutation Consent (writes/deletes/installs/sends/publishes may require confirmation depending on risk), Reversibility (prefer undoable actions; create backups before risky mutation), Dependency & Supply-Chain Caution (installing/executing/trusted external code requires elevated scrutiny), and Policy Preservation (project/user/platform constraints override convenience).

WARD runs alongside FUSE at each action decision point — FUSE selects the action, WARD gates whether it is allowed. For pure reads of project files with no boundary crossing or secret exposure, WARD does not fire. When it does fire, it can proceed, constrain, confirm, refuse, or require recovery.

---

## FLOW

### Why It Exists

FUSE governs which tools the agent calls and what their results prove — it does not evaluate the code those calls produce for operational drag. OWL's Simplicity principle catches over-engineering at implementation time, but it is a synchronous judgment: is this code simple now? It does not catch patterns that create ongoing operational cost — retry loops without backoff that cascade under load, unbounded queues that leak memory, N+1 queries that are simple to write and slow to run, abstractions that add maintenance burden without reducing complexity.

The gap matters because operational drag compounds. A retry storm that works in testing fails under load. An unbounded queue that holds 10 items in development holds 10,000 in production and leaks memory. An N+1 query that's fast with 100 records is slow with 10,000. These are not correctness bugs — the code works. They are efficiency defects that manifest at scale, under load, or over time — exactly when they're hardest to debug and most expensive to fix.

FLOW addresses this with eight principles: Retry Discipline (backoff, jitter, bounds, idempotency), Backpressure (bounded queues, producer respects consumer capacity), Cache Hygiene (invalidation strategy, stampede guards, no unnecessary caching), Startup Efficiency (fast startup, lazy loading), Hot-Path Awareness (algorithmic complexity in frequently-run code), External I/O Discipline (batching, pagination, no N+1), Workflow Friction (build/test/CI/dev cycle speed), and Maintenance Weight (abstractions must reduce more complexity than they add).

FLOW is triggered, not always-on. It only runs when the implementation touches one of ten trigger areas — retries, queues, caches, startup, hot paths, build/test/CI, provider/API calls, database/filesystem, complex abstractions, or maintenance burden. If no trigger fires, FLOW does not run at all. This is the primary suppression mechanism: a domain gate, not a weight threshold. The anti-goal is explicit — FLOW does not optimize for cleverness, micro-performance, or theoretical elegance. It only acts when implementation choices create measurable or likely operational drag.

FLOW runs after FUSE-governed execution produces the artifact, before DOX closeout. It evaluates the produced code — not the agent's tool use (that's FUSE's domain). FUSE and FLOW are orthogonal: FUSE governs the agent's execution; FLOW governs the code that execution produces. When FLOW surfaces a finding and the user accepts the tradeoff, ANCHOR's Action Accountability records the decision. DOX records it only if the tradeoff establishes a durable project constraint, recurring exception, or future implementation rule — not every accepted tradeoff belongs in AGENTS.md.

---

## Limitations

**All seven skills require frontier-class models to function as designed.** The gate logic in OWL and SISPIS, the state tracking in ANCHOR, the contract reasoning in DOX, the execution strategy gates in FUSE, and the trigger gate and efficiency pass in FLOW all require consistent multi-step conditional reasoning held across an entire conversation. On capable models (Claude Sonnet/Opus, GPT-4o, Gemini 1.5 Pro and above), the gates fire reliably. Further down the capability curve, three things degrade:

- *Instruction following fidelity* — the gate logic drifts mid-task. Models start applying surface-level pattern matching instead of running the arithmetic.
- *Metacognitive self-monitoring* — OWL's pressure layer asks a model to detect its own states: whether it's capitulating to social pressure, whether its approach has failed, whether context has drifted. ANCHOR's Recovery Discipline requires detecting approach failure and sunk cost accumulation. This requires introspective reasoning. Smaller models handle it poorly or not at all.
- *Structured output consistency* — OWL's signal schema, ANCHOR's checkpoint format, FUSE's evidence classification, FLOW's signal shape, and WARD's signal shape require reliably shaped output. Models with strong function-calling support produce this cleanly; others hallucinate the shape or drop fields under pressure.

On weaker models, all skills will partially apply — the principles are readable and some will be followed — but the gate consistency, state tracking, and pressure behavior should not be relied upon.

**The pipeline order matters, but the full pipeline is not always required.** The canonical order:

```
OWL preflight
  → ANCHOR state baseline
  → DOX load, if editing a DOX-governed project
  → FUSE + WARD wrap every action/tool decision
  → artifact/edit produced
  → FLOW evaluates artifact if trigger fires
  → DOX closeout, if docs/contracts changed
  → ANCHOR checkpoint/reclassification if needed
  → SISPIS final output
```

DOX-load happens before edits; DOX-closeout happens after FLOW, because FLOW may surface accepted tradeoffs that docs should record. FUSE + WARD are not a linear stage — they wrap every tool/action decision. FLOW runs only if a trigger area is touched. Loading them out of order loses signal elevation. SISPIS without OWL operates on native entropy scoring only. ANCHOR without OWL loses reasoning-signal triggers for recovery and state transitions. FUSE without OWL loses the definition of what needs verifying. FLOW without FUSE can still evaluate supplied artifacts, but it loses execution provenance: it cannot know which tools produced the artifact, what was verified, or what evidence the agent actually gathered. DOX without the others still preserves doc contracts correctly.

Apply the minimum subset that serves the task: OWL + SISPIS for single-turn tasks; OWL + ANCHOR + FUSE + WARD + FLOW + SISPIS for multi-turn implementation or debugging; full pipeline only when editing files in a DOX-enabled project. Running all seven on every request is unnecessary overhead and ANCHOR's Information Economy principle applies to the pipeline itself.

**The reference files are load-on-demand, not always in context.** All skills use a progressive disclosure architecture: SKILL.md is always loaded, reference files are consulted when needed. Cross-skill integration details live in `shared/integration.md`; adapter generation is driven by `shared/adapter-source.md`. When adapting these for platforms that inject everything into a single system prompt, the reference file content is not automatically included. For most uses, SKILL.md alone is sufficient. For the full signal registry, SISPIS entropy mapping, ANCHOR checkpoint format, FUSE execution strategy, FLOW operational efficiency signal registry, WARD authority boundaries, and pressure procedures, the reference files need to be included as well.

**These are behavioral protocols, not guardrails.** They shape how a model reasons, persists, and communicates. They don't prevent a model from doing something wrong — they increase the probability it catches the wrong thing before doing it, and surfaces the finding when it matters. A model can still make mistakes with these loaded. The goal is systematic reduction of a specific class of errors, not elimination.

---

## Directory Structure

```
CognitiveFrameWorks/
├── README.md
├── CLAUDE.md
├── plugin.json
├── shared/
│   ├── adapter-source.md
│   ├── integration.md
│   └── signals.md
├── scripts/
│   ├── generate-adapters.py
│   └── validate-framework.py
├── OWL/
│   ├── SKILL.md
│   ├── references/
│   │   ├── signal-schema.md
│   │   ├── pressure-protocol.md
│   │   └── cheatsheet.md
│   ├── examples/
│   │   ├── silent-execution.md
│   │   ├── reality-triggers.md
│   │   ├── epistemics-surfaces.md
│   │   ├── verification-triggers.md
│   │   ├── pressure-panic.md
│   │   ├── sycophancy-resistance.md
│   │   ├── reset-recovery.md
│   │   └── sispis-integration.md
│   └── adapters/
│       ├── system-prompt.md
│       ├── .cursorrules
│       ├── .windsurfrules
│       ├── CLAUDE.md
│       ├── .aider.conf.yml
│       └── continue-config.yaml
├── ANCHOR/
│   ├── SKILL.md
│   ├── references/
│   │   ├── execution-continuity.md
│   │   └── cheatsheet.md
│   ├── examples/
│   │   ├── checkpoint-recovery.md
│   │   ├── object-continuity.md
│   │   ├── recovery-discipline.md
│   │   └── epistemic-classification.md
│   └── adapters/
│       ├── system-prompt.md
│       ├── .cursorrules
│       ├── .windsurfrules
│       ├── CLAUDE.md
│       ├── .aider.conf.yml
│       └── continue-config.yaml
├── DOX/
│   ├── SKILL.md
│   ├── references/
│   │   ├── hierarchy-guide.md
│   │   └── closeout-protocol.md
│   ├── examples/
│   │   ├── root-agents.md
│   │   └── child-agents.md
│   └── adapters/
│       ├── system-prompt.md
│       ├── .cursorrules
│       ├── .windsurfrules
│       ├── CLAUDE.md
│       ├── .aider.conf.yml
│       └── continue-config.yaml
├── FUSE/
│   ├── SKILL.md
│   ├── references/
│   │   ├── execution-strategy.md
│   │   └── cheatsheet.md
│   ├── examples/
│   │   ├── silent-execution.md
│   │   ├── multi-signal-surface.md
│   │   ├── evidence-interpretation.md
│   │   ├── termination-recovery.md
│   │   ├── concurrency-decision.md
│   │   └── restraint-trigger.md
│   └── adapters/
│       ├── system-prompt.md
│       ├── .cursorrules
│       ├── .windsurfrules
│       ├── CLAUDE.md
│       ├── .aider.conf.yml
│       └── continue-config.yaml
├── FLOW/
│   ├── SKILL.md
│   ├── references/
│   │   ├── operational-efficiency.md
│   │   └── cheatsheet.md
│   ├── examples/
│   │   ├── silent-execution.md
│   │   ├── retry-storm-detection.md
│   │   ├── n-plus-one-query.md
│   │   ├── backpressure-detection.md
│   │   └── maintenance-weight.md
│   └── adapters/
│       ├── system-prompt.md
│       ├── .cursorrules
│       ├── .windsurfrules
│       ├── CLAUDE.md
│       ├── .aider.conf.yml
│       └── continue-config.yaml
├── WARD/
│   ├── SKILL.md
│   ├── references/
│   │   ├── authority-boundaries.md
│   │   └── cheatsheet.md
│   ├── examples/
│   │   ├── safe-read-vs-destructive-write.md
│   │   ├── secret-exposure.md
│   │   ├── trust-boundary-crossing.md
│   │   ├── confirmation-required.md
│   │   ├── sandbox-escalation.md
│   │   └── supply-chain-risk.md
│   └── adapters/
│       ├── system-prompt.md
│       ├── .cursorrules
│       ├── .windsurfrules
│       ├── CLAUDE.md
│       ├── .aider.conf.yml
│       └── continue-config.yaml
└── SISPIS/
    ├── SKILL.md
    ├── references/
    │   ├── response-schema.md
    │   ├── communication-framework.md
    │   └── cheatsheet.md
    ├── examples/
    │   ├── no-decision-mode.md
    │   ├── low-entropy-suppressed.md
    │   ├── medium-entropy-activation.md
    │   ├── high-entropy-hard-override.md
    │   ├── high-entropy-required.md
    │   ├── implicit-decision-detect.md
    │   ├── schema-bypass.md
    │   ├── user-override.md
    │   └── writing-task.md
    └── adapters/
        ├── system-prompt.md
        ├── .cursorrules
        ├── .windsurfrules
        ├── CLAUDE.md
        ├── .aider.conf.yml
        └── continue-config.yaml
```
