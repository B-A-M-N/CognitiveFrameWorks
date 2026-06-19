<!-- Generated from shared/adapter-source.md. Do not edit directly. -->

# FLOW — Friction, Load, Overhead, and Workload

## What This Does

FLOW evaluates the implementation for operational drag. It runs after FUSE-governed execution produces the artifact, before DOX closeout. Its question: *Does this change preserve smooth execution and avoid unnecessary drag?*

OWL owns reasoning integrity. ANCHOR owns state integrity. DOX owns contract integrity. FUSE owns execution integrity. SISPIS owns communication integrity. FLOW owns operational efficiency integrity.

## Anti-Goal

FLOW does not optimize for cleverness, micro-performance, or theoretical elegance.
FLOW only acts when implementation choices create measurable or likely operational drag.

## Trigger Gate

FLOW only activates when the task touches one or more of:
- retries / backoff / timeouts
- queues / streams / concurrency
- caches / memoization / invalidation
- startup / initialization
- hot paths / loops / scans
- build, test, CI, or dev workflow
- provider/API calls
- database or filesystem access
- complex abstractions
- long-lived maintenance burden

**No triggers → FLOW does not run.** This is the primary suppression mechanism.

---

## Two Modes

**Silent mode** (default): All eight principles applied internally. Nothing narrated. Output is the artifact with drag avoided.

**Surface mode**: One or more principles produced signals whose cumulative weight W_flow >= 1.5. The relevant findings appear before the artifact — one line each, no preamble.

---

## The Gate

```
Check trigger gate. If no triggers → FLOW does not run. W_flow = 0.
If triggers present, run all 8 principles against the implementation.
Each principle emits 0 or more signals, each with a weight (0.5, 1.0, or 2.0).

W_flow = sum of all emitted signal weights

If W_flow >= 1.5 → Surface mode
If W_flow <  1.5 → Silent mode
```

---

## Signal Shape

```
{ principle, signal_type, weight (0.5|1.0|2.0), finding, implication }
```

`finding` = what was observed in the code. `implication` = the operational consequence — what will slow down, break under load, or require ongoing maintenance.

---

## The Eight Principles

### 1. Retry Discipline — Retries must have backoff, jitter, and bounds. Non-idempotent operations must not be retried blindly.
Surface when: retry loop lacks backoff or uses fixed delay (retry storm risk); operation retried without verifying idempotency; external call has no timeout.
Under pressure: don't skip backoff "to keep it simple." Retry storms are worse than no retry. Backoff with jitter is the minimum viable retry.
Signals: `retry_storm_risk` (2.0), `non_idempotent_retry` (1.0), `missing_timeout` (1.0)

### 2. Backpressure — Queues and streams must have bounds. Producers must respect consumer capacity.
Surface when: queue or buffer has no size limit; producer doesn't check consumer capacity; stream has no flow control.
Under pressure: don't add unbounded buffers "for safety." Unbounded accumulation is a memory leak that manifests under load.
Signals: `unbounded_accumulation` (2.0), `missing_flow_control` (1.0)

### 3. Cache Hygiene — Caching must have an invalidation strategy. Cache complexity must be proportional to the benefit.
Surface when: cache has no invalidation or TTL; invalidation pattern allows thundering herd; caching added where computation is trivially cheap.
Under pressure: don't cache everything "for speed." Unnecessary caching adds invalidation complexity and stale-data risk for zero benefit.
Signals: `cache_stampede_risk` (2.0), `stale_cache_risk` (1.0), `unnecessary_caching` (0.5)

### 4. Startup Efficiency — Startup must be fast. Expensive work must be deferred or lazy-loaded.
Surface when: expensive synchronous work in startup or module-load path; resources eagerly loaded where lazy loading would avoid cost.
Under pressure: don't eager-load "to be safe." Blocking startup affects every cold start, test run, and deploy.
Signals: `blocking_startup` (1.0), `eager_loading` (0.5)

### 5. Hot-Path Awareness — Hot paths must avoid algorithmic drag. Loops and scans must be efficient for the expected scale.
Surface when: hot path uses O(n²) or worse where O(n) suffices; same computation repeated in loop where it could be hoisted; full scan where direct lookup would work.
Under pressure: don't ignore O(n²) "because n is small." n grows. Use the right algorithm from the start.
Signals: `algorithmic_drag` (1.0), `repeated_computation` (0.5)

### 6. External I/O Discipline — External calls must be batched, paginated, and non-blocking where the volume warrants.
Surface when: N+1 query pattern (query in a loop); external fetch has no pagination or limit; synchronous blocking I/O on stall path; sequential calls where batching is trivially available.
Under pressure: don't fetch one-by-one "because it's simpler." N+1 is simpler to write and slower to run — simplicity borrowed against operational cost.
Signals: `n_plus_one_query` (2.0), `missing_pagination` (1.0), `sync_blocking_io` (1.0), `missing_batching` (0.5)

### 7. Workflow Friction — The change must not slow the build, test, or dev cycle. Feedback loops must stay fast.
Surface when: change introduces friction in build/test/CI/dev workflow; change prevents incremental build or test; synchronous step added to previously async pipeline.
Under pressure: don't add synchronous steps "because it's correct." Slow CI is operational drag affecting every developer on every push.
Signals: `workflow_friction` (1.0), `missing_incremental` (0.5)

### 8. Maintenance Weight — Abstractions and patterns must reduce more complexity than they add. Maintenance cost must be proportional to benefit.
Surface when: abstraction added that doesn't reduce complexity at call site; change introduces tight coupling complicating future modifications; pattern introduced where maintenance cost exceeds benefit.
Under pressure: don't add abstractions "for flexibility." Unused flexibility is maintenance cost with no benefit. Add abstraction when there's a concrete second use case.
Signals: `coupling_burden` (1.0), `unnecessary_abstraction` (0.5)

---

## Surface Format

```
**[Principle]:** [finding]. [implication.]

[artifact with drag addressed, or explicit tradeoff noted]
```

Multiple signals stack by descending weight before the artifact. No preamble.

## When Not to Surface

No trigger area touched → FLOW does not run. Finding is theoretical improvement, not operational drag. Drag negligible at expected scale. Tradeoff explicitly accepted. W_flow < 1.5. Default: proceed silently.

---

## Integration Points

### OWL → FLOW: OWL says "this is complex" or "this changes behavior." FLOW evaluates whether that complexity creates operational drag.
### FUSE → FLOW: Orthogonal. FUSE governs the agent's tool use; FLOW governs the code the agent produces.
### FLOW → ANCHOR: FLOW does not trigger Recovery Discipline. Accepted tradeoffs are recorded via Action Accountability.
### FLOW → SISPIS: FLOW findings elevate entropy. `retry_storm_risk`, `unbounded_accumulation`, `n_plus_one_query`, `cache_stampede_risk` each add +2 to `downstream_impact` and `tradeoff_density`. See `references/operational-efficiency.md` for full mapping.
### FLOW → DOX: Accepted maintenance burdens are recorded in AGENTS.md during DOX closeout.

## Pipeline Position

```
Request → OWL → ANCHOR → DOX(load) → FUSE → Edit → FLOW → DOX(closeout) → SISPIS → Output
```

FLOW runs once per artifact, evaluating the produced code for operational drag.

## Budget Rule

The trigger gate is the primary budget mechanism. A task touching one trigger area gets a targeted pass on the relevant principle, not a full eight-principle sweep. Suppress when: only one trigger area touched and relevant principle is obvious; change is to existing pattern that already follows the principle; drag negligible at expected scale.
