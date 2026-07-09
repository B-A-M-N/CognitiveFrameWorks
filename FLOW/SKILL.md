---
name: flow
description: Friction, Load, Overhead, and Workload — evaluates implementation for operational drag across eight principles: Retry Discipline, Backpressure, Cache Hygiene, Startup Efficiency, Hot-Path Awareness, External I/O Discipline, Workflow Friction, and Maintenance Weight. Triggered, not always-on. FLOW activates only when a task touches retry/backoff/timeout, queues/streams/concurrency, caches/memoization/invalidation, startup/initialization, hot paths/loops/scans, build/test/CI/dev workflow, provider/API calls, database/filesystem access, complex abstractions, or long-lived maintenance burden. FLOW does not optimize for cleverness, micro-performance, or theoretical elegance — it only acts when implementation choices create measurable or likely operational drag. Runs after FUSE-governed execution, before DOX closeout.
---

# FLOW — Friction, Load, Overhead, and Workload

## What This Does

FLOW evaluates the implementation for operational drag. It runs after FUSE-governed execution produces the artifact, before DOX closeout. Its question: *Does this change preserve smooth execution and avoid unnecessary drag?*

OWL owns reasoning integrity. ANCHOR owns state integrity. DOX owns contract integrity. FUSE owns execution integrity. SISPIS owns communication integrity. FLOW owns operational efficiency integrity — whether the produced code creates avoidable friction, load, overhead, or workload.

Full signal registry, trigger criteria, and integration spec: `references/operational-efficiency.md`. Quick lookup: `references/cheatsheet.md`.

---

## Anti-Goal

FLOW does not optimize for cleverness, micro-performance, or theoretical elegance.
FLOW only acts when implementation choices create measurable or likely operational drag.

A finding that something could be "faster" or "more elegant" is not a FLOW finding. A finding that a retry loop without backoff will cascade under load is a FLOW finding. The distinction: measurable or likely operational drag vs. theoretical improvement.

---

## Trigger Gate

FLOW is triggered, not always-on. It only activates when the task touches one or more of these areas:

- **Retries / backoff / timeouts** — retry loops, exponential backoff, circuit breakers
- **Queues / streams / concurrency** — message queues, event streams, thread pools, semaphores
- **Caches / memoization / invalidation** — in-memory caches, CDN, memoized results, TTL strategies
- **Startup / initialization** — module load, app boot, cold starts, eager vs lazy loading
- **Hot paths / loops / scans** — frequently executed code, inner loops, full scans, repeated lookups
- **Build, test, CI, or dev workflow** — build time, test suite speed, CI pipeline, dev cycle friction
- **Provider/API calls** — external service calls, rate limits, pagination, batching
- **Database or filesystem access** — queries, index usage, file I/O, connection pooling
- **Complex abstractions** — abstraction layers, indirection, coupling, pattern introduction
- **Long-lived maintenance burden** — code that will require ongoing upkeep, coupling that complicates future changes

If none of these triggers are present, FLOW does not run. This is the primary suppression mechanism — not a weight threshold, but a domain gate.

---

## Two Modes

**Silent mode** (default): All eight principles applied internally. Nothing narrated. Output is the artifact with drag avoided.

**Surface mode**: One or more principles produced signals with medium or higher severity. The relevant findings appear before the artifact — one line each, no preamble.

Surface mode is not a tone shift. It is a notification that the implementation creates operational drag the user would want to know about.

---

## The Gate

Each principle emits signals during the efficiency pass. When cumulative signal weight crosses the surface threshold, the relevant findings appear.

```
Check trigger gate. If no triggers → FLOW does not run.
If triggers present, run all 8 principles against the implementation.
Each principle emits 0 or more signals, each with a severity: low | medium | high.

If any signal has high severity → Surface mode
If cumulative medium-severity signals dominate → Surface mode
If all signals are low → Silent mode

Full signal registry and severity classification: `references/operational-efficiency.md`. Numeric scoring rubrics are maintained in references only — the SKILL.md uses qualitative gates.
```

Full signal registry and weights: `references/operational-efficiency.md`.

---

## Signal Shape

Every emitted signal resolves to this structure before the gate runs:

```
{
  "principle": "<principle name>",
  "signal_type": "<type from registry>",
  "severity": "low" | "medium" | "high",
  "finding": "What the efficiency pass found in the implementation.",
  "implication": "What operational drag this creates if not addressed."
}
```

`finding` is what was observed in the code. `implication` is the operational consequence — what will slow down, break under load, or require ongoing maintenance. These are always distinct.

---

## The Eight Principles

Each principle lists: the default behavior, the surface condition, and the pressure variant.

---

### 1. Retry Discipline
*Retries must have backoff, jitter, and bounds. Non-idempotent operations must not be retried blindly.*

**Default:** When implementing retry logic, include exponential backoff with jitter to avoid thundering herd, a maximum retry count to prevent infinite loops, and a timeout to prevent indefinite hangs. Verify the operation is idempotent before retrying — non-idempotent retries create duplicates.

**Surface when:** A retry loop lacks backoff or uses fixed delay (retry storm risk under load). An operation is being retried without verifying idempotency. An external call has no timeout, allowing indefinite hang.

**Under pressure:** Under simplicity pressure, the temptation is to write a bare `for` loop with `retry()` and no backoff. "Keep it simple" produces a retry storm that cascades under load. Backoff with jitter is not complexity — it is the minimum viable retry.

**Signal types:** `retry_storm_risk`, `non_idempotent_retry`, `missing_timeout`

---

### 2. Backpressure
*Queues and streams must have bounds. Producers must respect consumer capacity.*

**Default:** When implementing queues, buffers, or streams, set a maximum size or apply backpressure when the consumer can't keep up. Unbounded accumulation is a memory leak that manifests under load, not in testing. Producers that don't react to consumer slowness create unbounded growth.

**Surface when:** A queue or buffer has no size limit. A producer doesn't check consumer capacity before enqueuing. A stream has no flow control mechanism.

**Under pressure:** Under safety pressure, the temptation is to add an unbounded buffer "to never lose messages." Unbounded accumulation is a memory leak with extra steps. A bounded queue with a defined overflow policy is the correct pattern.

**Signal types:** `unbounded_accumulation`, `missing_flow_control`

---

### 3. Cache Hygiene
*Caching must have an invalidation strategy. Cache complexity must be proportional to the benefit.*

**Default:** When adding a cache, define how entries are invalidated — TTL, explicit invalidation, or event-driven. A cache without invalidation serves stale data. A cache without a stampede guard allows thundering herd on miss. Don't add caching where the computation is cheap or the hit rate would be low — the complexity isn't justified.

**Surface when:** A cache has no invalidation strategy or TTL. A cache invalidation pattern allows thundering herd (no lock on miss, no jitter). Caching is being added where the computation is trivially cheap.

**Under pressure:** Under performance pressure, the temptation is to cache everything "for speed." Unnecessary caching adds invalidation complexity, memory overhead, and stale-data risk — all for a benefit that may be zero on a cheap computation.

**Signal types:** `cache_stampede_risk`, `stale_cache_risk`, `unnecessary_caching`

---

### 4. Startup Efficiency
*Startup must be fast. Expensive work must be deferred or lazy-loaded.*

**Default:** Module load and application startup should be fast. Expensive initialization — large file reads, network calls, heavy computation — should be deferred until needed or done lazily. Blocking startup affects every cold start, every test run, every deploy.

**Surface when:** Expensive synchronous work is in the startup or module-load path. Resources are being eagerly loaded where lazy loading would avoid the cost. Startup time is disproportionate to the work being done.

**Under pressure:** Under correctness pressure, the temptation is to eager-load everything "to be safe." Blocking startup affects every cold start — development, testing, deployment. Lazy loading with a clear initialization contract is the correct pattern.

**Signal types:** `blocking_startup`, `eager_loading`

---

### 5. Hot-Path Awareness
*Hot paths must avoid algorithmic drag. Loops and scans must be efficient for the expected scale.*

**Default:** Code that runs frequently or processes large inputs must use appropriate algorithms. O(n²) in a hot path becomes O(n² × traffic). Repeated computation in a loop should be hoisted or memoized. Full scans where an index or lookup would suffice create unnecessary load.

**Surface when:** A hot path uses O(n²) or worse where O(n) or O(n log n) suffices. The same computation is repeated in a loop where it could be hoisted or memoized. A full scan is used where a direct lookup would work.

**Under pressure:** Under "n is small" pressure, the temptation is to ignore algorithmic complexity. n grows. A loop that's fast with 10 items is slow with 10,000, and the code is in production before anyone notices. Use the right algorithm from the start.

**Signal types:** `algorithmic_drag`, `repeated_computation`

---

### 6. External I/O Discipline
*External calls must be batched, paginated, and non-blocking where the volume warrants.*

**Default:** When calling external services — APIs, databases, filesystems — batch multiple calls into one where possible, paginate unbounded fetches, and avoid blocking I/O on paths where it causes stalls. N+1 query patterns (one query per item in a loop) are a classic drag source. Sequential calls where batching is trivially available waste latency.

**Surface when:** An N+1 query pattern is being used (query in a loop). An external fetch has no pagination or limit. Synchronous blocking I/O is on a path where it causes stalls. Sequential calls are being made where batching is trivially available and the volume warrants it.

**Under pressure:** Under simplicity pressure, the temptation is to fetch one-by-one "because it's simpler." N+1 queries are simpler to write and slower to run — the simplicity is borrowed against operational cost. Batch from the start when the volume is non-trivial.

**Signal types:** `n_plus_one_query`, `missing_pagination`, `sync_blocking_io`, `missing_batching`

---

### 7. Workflow Friction
*The change must not slow the build, test, or dev cycle. Feedback loops must stay fast.*

**Default:** When adding code, consider its impact on the development workflow. Does it slow the build? Does it make tests slower? Does it add a synchronous step to CI? Developer time is operational cost. Slow feedback loops compound — every developer pays the tax on every cycle.

**Surface when:** A change introduces friction in the build, test, CI, or dev workflow. A change prevents incremental build or test where it was previously possible. A synchronous step is being added to a previously asynchronous pipeline.

**Under pressure:** Under correctness pressure, the temptation is to add synchronous steps "because it's correct." Slow CI is operational drag that affects every developer on every push. Find the asynchronous alternative or accept the cost explicitly.

**Signal types:** `workflow_friction`, `missing_incremental`

---

### 8. Maintenance Weight
*Abstractions and patterns must reduce more complexity than they add. Maintenance cost must be proportional to benefit.*

**Default:** When adding abstractions, patterns, or indirection, verify they reduce more complexity than they introduce. An abstraction that requires ongoing updates without simplifying the calling code is net negative. Tight coupling that complicates future changes creates maintenance burden. The test: will this code be easier or harder to change in six months?

**Surface when:** An abstraction is being added that doesn't reduce complexity at the call site. A change introduces tight coupling that will complicate future modifications. A pattern is being introduced where the maintenance cost exceeds the benefit.

**Under pressure:** Under flexibility pressure, the temptation is to add abstractions "for future use." Unused flexibility is maintenance cost with no benefit. Add abstraction when there's a concrete second use case, not when there's a hypothetical one.

**Signal types:** `coupling_burden`, `unnecessary_abstraction`

---

## Surface Format

When the severity gate fires (medium or higher), surface signals before the artifact. Order by descending severity.

```
**[Principle]:** [finding]. [implication.]

[artifact with drag addressed, or explicit tradeoff noted]
```

If multiple signals surface, stack them — one per line — before the artifact. No preamble. No enumeration of principles that didn't fire.

---

## When Not to Surface

- No trigger area is touched → FLOW does not run
- The finding is a theoretical improvement, not operational drag
- The drag is negligible at the expected scale
- The tradeoff was explicitly accepted by the user
- All signals are low severity

When in doubt: proceed silently. FLOW's anti-goal is to become a general code review skill. It is not.

---

## Relationship to Other Skills

### OWL → FLOW

OWL answers: *Is this correct?*
FLOW answers: *Is this creating avoidable drag?*

OWL's Simplicity principle says "minimum code that solves the problem." FLOW's Maintenance Weight says "abstractions must reduce more complexity than they add." These overlap on surface but differ in focus: OWL's Simplicity is a synchronous judgment (is this code simple now?); FLOW's Maintenance Weight is a longitudinal judgment (will this code create drag over time?). OWL catches over-engineering at implementation time. FLOW catches patterns that create ongoing operational cost. Both can fire on the same code for different reasons.

### FUSE → FLOW

FUSE answers: *What actions should I run, and what do they prove?*
FLOW answers: *Does the implementation those actions produced create operational drag?*

FUSE governs the agent's own tool use — which tools to call, in what order, what results prove. FLOW governs the code the agent produces — does the implementation create friction, load, overhead, or workload? These are completely orthogonal. FUSE's Resource Bounds governs timeout/scope on the agent's tool calls; FLOW's Retry Discipline governs retry/timeout in the code being written. Different domains.

### WARD → FLOW

WARD answers: *Is this action allowed and safe enough?*
FLOW answers: *Does the code this action produces create operational drag?*

When WARD accepts a risk-laden tradeoff (e.g., the user confirms a retry strategy that hammers an external API, or accepts an unbounded queue in a security-sensitive path), WARD feeds the accepted tradeoff to FLOW. FLOW evaluates whether the accepted authority/security tradeoff creates ongoing operational burden. Example: WARD allows an aggressive retry loop against an external API because the user confirmed it; FLOW's Retry Discipline evaluates whether the retry loop will cascade under load. WARD owns whether the tradeoff is allowed. FLOW owns whether the accepted tradeoff creates ongoing operational drag.

### FLOW → ANCHOR

FLOW answers: *Does this create drag?*
ANCHOR answers: *What state did the actions produce?*

FLOW findings are about the artifact, not the execution state. FLOW does not trigger ANCHOR's Recovery Discipline (that's FUSE/OWL's domain for execution failures). However, when FLOW surfaces a finding and the user accepts the tradeoff (the drag is worth it for some other benefit), ANCHOR's Action Accountability records the decision and its rationale — the accepted drag becomes a tracked state item.

### FLOW → SISPIS

FLOW answers: *What operational drag does this create?*
SISPIS answers: *How should this be communicated?*

FLOW findings feed SISPIS's entropy score. A `retry_storm_risk` or `n_plus_one_query` finding elevates entropy — the output must frame the tradeoff, not just deliver the code. A `workflow_friction` finding elevates `tradeoff_density` — the user needs to know the dev cycle cost.

| FLOW signal | SISPIS signal | Delta |
|-------------|---------------|-------|
| `retry_storm_risk` | `downstream_impact`, `tradeoff_density` | +2 each |
| `unbounded_accumulation` | `downstream_impact`, `tradeoff_density` | +2 each |
| `n_plus_one_query` | `downstream_impact`, `tradeoff_density` | +2 each |
| `cache_stampede_risk` | `downstream_impact`, `tradeoff_density` | +2 each |
| `non_idempotent_retry` | `tradeoff_density` | +1 |
| `missing_timeout` | `downstream_impact` | +1 |
| `missing_flow_control` | `downstream_impact` | +1 |
| `stale_cache_risk` | `downstream_impact` | +1 |
| `blocking_startup` | `downstream_impact` | +1 |
| `algorithmic_drag` | `downstream_impact` | +1 |
| `missing_pagination` | `downstream_impact` | +1 |
| `sync_blocking_io` | `downstream_impact` | +1 |
| `workflow_friction` | `tradeoff_density` | +1 |
| `coupling_burden` | `downstream_impact`, `tradeoff_density` | +1 each |
Sub-threshold deltas (+0.5) and the complete mapping: `references/operational-efficiency.md`.

Apply as upstream signal inputs to SISPIS. SISPIS collects all upstream signals, deduplicates by underlying cause (highest severity per cause wins), sums remaining deltas, and caps each dimension at 2.0. See CLAUDE.md § SISPIS Signal Integration Protocol for the full deduplication rule.

### DOX ↔ FLOW

FLOW answers: *Does this create operational drag?*
DOX answers: *What documentation contract applies?*

When FLOW surfaces a finding and the user accepts the tradeoff, ANCHOR's Action Accountability records the decision. DOX's closeout records it in the relevant AGENTS.md only if the tradeoff establishes a durable project constraint, recurring exception, or future implementation rule. A prototype-scoped exception (\"Use an unbounded queue here temporarily because this is a prototype\") is ANCHOR-only; a durable rule (\"Provider adapters must use bounded queues with backpressure because this path has caused retry storms\") belongs in AGENTS.md.

---

## Pipeline Position

FLOW runs after FUSE + WARD-governed execution produces the artifact, before DOX closeout.

```
Request
  → OWL (pre-implementation reasoning pass)
  → ANCHOR (operational persistence setup)
  → DOX (load documentation contracts)
  → FUSE + WARD (wrap execution — per action)
  → Edit (artifact produced)
  → FLOW (operational efficiency evaluation)      ← evaluates the artifact
  → DOX (closeout pass)
  → ANCHOR (checkpoint/reclassification if needed)
  → SISPIS (decision-routing and response calibration)
  → Output
```

FLOW runs once per artifact, not per tool call. It evaluates the produced code for operational drag across the eight principles. If drag is found, the fix is applied before DOX closeout.

---

## Budget Rule

FLOW applies the same Information Economy principle as the other skills: the smallest sufficient governance. The trigger gate is the primary budget mechanism — if no trigger area is touched, FLOW doesn't run at all.

When triggers are present but the change is small (e.g., adding a single timeout to an existing call), the full eight-principle pass is overhead. FUSE overhead scales with action complexity; FLOW overhead scales with the breadth of operational surface touched. A change that touches one trigger area gets a targeted pass on the relevant principle, not a full eight-principle sweep.

Do not run the full pass when:
- Only one trigger area is touched and the relevant principle is obvious
- The change is to an existing pattern that already follows the principle
- The drag is negligible at the expected scale and no signal would fire

---

## Additional Resources

### Reference Files

- **`references/operational-efficiency.md`** — Complete signal registry, trigger criteria, decision guidance per principle, integration specs
- **`references/cheatsheet.md`** — Quick-reference summary of principles, signals, and integration deltas

### Example Files

Working examples in `examples/`:

- **`silent-execution.md`** — Change that doesn't touch any triggers, FLOW doesn't run
- **`retry-storm-detection.md`** — Retry without backoff detected, retry_storm_risk fires
- **`n-plus-one-query.md`** — N+1 query pattern in a loop detected
- **`backpressure-detection.md`** — Unbounded queue detected, unbounded_accumulation fires
- **`maintenance-weight.md`** — Abstraction that adds coupling without reducing complexity
