# FLOW Operational Efficiency — Integration Spec

## Signal Type Registry

Complete registry of all signal types, organized by principle. Multiple types per principle — a principle can emit any signal from its list in a single efficiency pass.

### Retry Discipline

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `retry_storm_risk` | 2.0 | A retry loop lacks exponential backoff with jitter, or uses fixed delay — under load, synchronized retries will cascade |
| `non_idempotent_retry` | 1.0 | An operation is being retried without verifying idempotency — retries on non-idempotent operations create duplicates |
| `missing_timeout` | 1.0 | An external call has no timeout — the call can hang indefinitely, blocking the caller |

### Backpressure

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `unbounded_accumulation` | 2.0 | A queue, buffer, or stream has no size limit or overflow policy — under load, memory grows without bound |
| `missing_flow_control` | 1.0 | A producer doesn't react to consumer slowness — it continues producing regardless of capacity, leading to unbounded growth or dropped work |

### Cache Hygiene

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `cache_stampede_risk` | 2.0 | A cache invalidation pattern allows thundering herd — no lock on miss, no jitter, so multiple callers simultaneously recompute the same value |
| `stale_cache_risk` | 1.0 | A cache has no invalidation strategy or TTL — entries persist indefinitely, serving stale data |
| `unnecessary_caching` | 0.5 | Caching is being added where the computation is trivially cheap or the hit rate would be low — the complexity isn't justified by the benefit |

### Startup Efficiency

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `blocking_startup` | 1.0 | Expensive synchronous work is in the startup or module-load path — every cold start, test run, and deploy pays the cost |
| `eager_loading` | 0.5 | Resources are being loaded eagerly at startup where lazy loading would avoid the cost without changing correctness |

### Hot-Path Awareness

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `algorithmic_drag` | 1.0 | A hot path uses O(n²) or worse where O(n) or O(n log n) suffices — the cost scales quadratically with input |
| `repeated_computation` | 0.5 | The same computation is repeated in a loop where it could be hoisted or memoized — redundant work on each iteration |

### External I/O Discipline

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `n_plus_one_query` | 2.0 | An N+1 query pattern is being used — one query per item in a loop, where a single batched query would suffice |
| `missing_pagination` | 1.0 | An external fetch has no pagination or limit — the fetch can return unbounded results, consuming memory and latency |
| `sync_blocking_io` | 1.0 | Synchronous blocking I/O is on a path where it causes stalls — the calling thread waits when it could continue |
| `missing_batching` | 0.5 | Sequential individual calls are being made where batching is trivially available and the volume warrants it — wasted latency on low-volume sequential calls |

### Workflow Friction

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `workflow_friction` | 1.0 | A change introduces friction in the build, test, CI, or dev workflow — slower feedback loops that affect every developer |
| `missing_incremental` | 0.5 | A change prevents incremental build or test where it was previously possible — the full pipeline must run for every change |

### Maintenance Weight

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `coupling_burden` | 1.0 | A change introduces tight coupling that will complicate future modifications — changes in one place require changes in another |
| `unnecessary_abstraction` | 0.5 | An abstraction is being added that doesn't reduce complexity at the call site — the abstraction adds indirection without benefit |

---

## Weight Summary

| Weight | Signal Types |
|--------|-------------|
| 2.0 | `retry_storm_risk`, `unbounded_accumulation`, `n_plus_one_query`, `cache_stampede_risk` |
| 1.0 | `non_idempotent_retry`, `missing_timeout`, `missing_flow_control`, `stale_cache_risk`, `blocking_startup`, `algorithmic_drag`, `missing_pagination`, `sync_blocking_io`, `workflow_friction`, `coupling_burden` |
| 0.5 | `unnecessary_caching`, `eager_loading`, `repeated_computation`, `missing_batching`, `missing_incremental`, `unnecessary_abstraction` |

The 2.0 weight is reserved for signals that represent cascading operational hazards — patterns that don't just create drag but actively compound under load. A retry storm cascades. An unbounded queue leaks memory progressively. An N+1 query scales with traffic. A cache stampede multiplies load. These are qualitatively different from a missing timeout or a slow build — they get worse nonlinearly.

---

## Gate Threshold

```
Trigger gate: if no trigger area is touched, FLOW does not run. W_flow = 0.

If triggers present:
  W_flow = sum of all emitted signal weights

  W_flow >= 1.5  →  Surface mode
  W_flow <  1.5  →  Silent mode
```

A single weight-2.0 signal always triggers surfacing. Two weight-1.0 signals trigger surfacing. One weight-1.0 + one weight-0.5 triggers surfacing (1.5 >= 1.5). Three weight-0.5 signals trigger surfacing (1.5 >= 1.5).

---

## Trigger Criteria

FLOW only runs when the task touches one or more trigger areas. The trigger check is the primary gate — before any principle is evaluated, the implementation is checked for trigger presence.

### Trigger Detection

| Trigger Area | Detected When |
|--------------|---------------|
| Retries / backoff / timeouts | Code adds or modifies retry loops, backoff logic, circuit breakers, or timeout handling |
| Queues / streams / concurrency | Code adds or modifies message queues, event streams, thread pools, semaphores, or concurrent data structures |
| Caches / memoization / invalidation | Code adds or modifies caching, memoization, TTL strategies, or cache invalidation |
| Startup / initialization | Code adds or modifies module load, app boot, cold start paths, or initialization sequences |
| Hot paths / loops / scans | Code adds or modifies frequently executed paths, inner loops, full scans, or lookup patterns |
| Build, test, CI, or dev workflow | Code changes build configuration, test setup, CI pipeline, or development workflow |
| Provider/API calls | Code adds or modifies external service calls, API integrations, or rate-limited endpoints |
| Database or filesystem access | Code adds or modifies database queries, index usage, file I/O, or connection pooling |
| Complex abstractions | Code adds new abstraction layers, indirection, design patterns, or coupling between modules |
| Long-lived maintenance burden | Code introduces patterns requiring ongoing upkeep across locations — duplicated config/constants that must stay in sync, hardcoded values spread across modules, or coupling that forces coordinated edits on every change |

### Trigger-to-Principle Map

The trigger gate determines which principles are worth evaluating. Not all eight principles need to run on every triggered task.

| Trigger Area | Principles to Evaluate |
|--------------|----------------------|
| Retries / backoff / timeouts | Retry Discipline |
| Queues / streams / concurrency | Backpressure |
| Caches / memoization / invalidation | Cache Hygiene |
| Startup / initialization | Startup Efficiency |
| Hot paths / loops / scans | Hot-Path Awareness |
| Provider/API calls | External I/O Discipline |
| Database or filesystem access | External I/O Discipline, Hot-Path Awareness |
| Build, test, CI, or dev workflow | Workflow Friction |
| Complex abstractions | Maintenance Weight |
| Long-lived maintenance burden | Maintenance Weight |

A task touching multiple trigger areas evaluates multiple principles. A task touching only one trigger area evaluates only the relevant principle — the full eight-principle pass is overhead.

---

## Decision Guidance Per Principle

### Retry Discipline

**Correct retry pattern:**
- Exponential backoff with jitter (full jitter or decorrelated jitter)
- Maximum retry count (3-5 for most cases)
- Timeout on each attempt
- Idempotency verified before retry

**Anti-patterns:**
- Bare `for` loop with `retry()` and no delay → `retry_storm_risk`
- Fixed delay (e.g., `sleep(1)` between retries) → `retry_storm_risk` (synchronized retries)
- Retry on POST/PUT without idempotency key → `non_idempotent_retry`
- HTTP call without timeout → `missing_timeout`

**The distinction FLOW draws:** A retry without backoff is not "suboptimal" — it is an operational hazard. Under load, all retrying clients synchronize their retries, creating a cascading failure. This is measurable, not theoretical.

### Backpressure

**Correct backpressure pattern:**
- Bounded queue with defined overflow policy (drop, block, or reject)
- Producer checks capacity before enqueuing
- Flow control on streams (backpressure signals)

**Anti-patterns:**
- `collections.deque()` with no maxlen → `unbounded_accumulation`
- `asyncio.Queue()` with no maxsize → `unbounded_accumulation`
- Producer that never checks queue size → `missing_flow_control`

**The distinction FLOW draws:** An unbounded queue is not "flexible" — it is a memory leak that manifests under load. The fix is a bounded queue with a defined overflow policy, not a larger buffer.

### Cache Hygiene

**Correct cache pattern:**
- TTL or explicit invalidation on every cached entry
- Stampede guard (lock on miss, or jitter on refresh)
- Cache only computations that are expensive relative to the cache lookup cost

**Anti-patterns:**
- `@lru_cache()` on a function that does a dict lookup → `unnecessary_caching`
- Cache with no TTL and no invalidation → `stale_cache_risk`
- Cache refresh where all clients refresh simultaneously → `cache_stampede_risk`

**The distinction FLOW draws:** Caching a cheap computation is not "an optimization" — it adds invalidation complexity and memory overhead for zero benefit. The cache lookup may cost more than the computation it replaces.

### Startup Efficiency

**Correct startup pattern:**
- Defer expensive initialization until first use (lazy loading)
- Async initialization where the framework supports it
- Module-load code does minimal work — heavy setup happens in explicit init

**Anti-patterns:**
- Reading a large config file at module load → `blocking_startup`
- Establishing DB connection at import time → `blocking_startup`
- Loading all locale files at startup when only one is needed → `eager_loading`

**The distinction FLOW draws:** Blocking startup is not "thorough" — it taxes every cold start, every test run, every deploy. Lazy loading with a clear init contract is the correct pattern.

### Hot-Path Awareness

**Correct hot-path pattern:**
- Appropriate algorithm for the expected scale
- Hoist invariant computations out of loops
- Use dict/set lookups (O(1)) instead of list scans (O(n)) for membership tests

**Anti-patterns:**
- Nested loop over the same list → `algorithmic_drag`
- `if item in list:` in a loop → `algorithmic_drag` (use a set)
- Recomputing `len(data)` or `expensive_func()` inside a loop → `repeated_computation`

**The distinction FLOW draws:** O(n²) in a hot path is not "fine for now" — n grows, and the code is in production before anyone notices. The algorithm should be correct from the start. However, FLOW does not flag O(n) where O(1) is theoretically possible but the n is bounded and small — that is micro-optimization, which is FLOW's anti-goal.

### External I/O Discipline

**Correct I/O pattern:**
- Batch multiple calls into one (bulk query, bulk API call)
- Paginate unbounded fetches
- Async I/O on paths where blocking causes stalls

**Anti-patterns:**
- Query in a loop (one SELECT per item) → `n_plus_one_query`
- `requests.get(url)` with no timeout → `missing_timeout` (also Retry Discipline)
- `SELECT * FROM table` with no LIMIT → `missing_pagination`
- Synchronous file read on a request handler → `sync_blocking_io`

**The distinction FLOW draws:** N+1 queries are not "simpler" — they are simpler to write and slower to run. The simplicity is borrowed against operational cost. However, FLOW does not flag sequential calls where the volume is trivially small (e.g., 3 calls) — that is where `missing_batching` (0.5) applies, and it may not surface alone.

### Workflow Friction

**Correct workflow pattern:**
- Keep build fast (incremental compilation, dependency caching)
- Keep test suite fast (parallel execution, targeted runs)
- Keep CI fast (cache dependencies, parallelize jobs)

**Anti-patterns:**
- Adding a synchronous lint step to every CI run where it could be async → `workflow_friction`
- Change that forces full rebuild where incremental was possible → `missing_incremental`
- Adding a slow integration test to the unit test suite → `workflow_friction`

**The distinction FLOW draws:** Slow CI is not "thorough" — it is operational drag that affects every developer on every push. The cost compounds across the team.

### Maintenance Weight

**Correct abstraction pattern:**
- Abstract when there's a concrete second use case
- Abstract when the call site is simpler with the abstraction than without
- Coupling is intentional and documented

**Anti-patterns:**
- Adding a factory layer when there's one implementation → `unnecessary_abstraction`
- Adding a config system for values that are hardcoded and stable → `unnecessary_abstraction`
- Change that couples two previously independent modules → `coupling_burden`

**The distinction FLOW draws:** An abstraction without a second use case is not "flexible" — it is maintenance cost with no benefit. The test: will this code be easier or harder to change in six months? If harder, the abstraction is net negative.

---

## Stacking Rules

Multiple signals can surface in a single response. Rules for stacking:

1. **Order by descending weight.** Higher-weight signals appear first — 2.0 signals (cascading hazards) before 1.0 (measurable drag) before 0.5 (minor overhead).
2. **Suppress redundant signals from the same principle.** If `retry_storm_risk` surfaces, do not also surface `missing_timeout` from the same retry loop — the storm risk implies the timeout is also missing.
3. **Don't merge finding + implication across signals.** Each signal line is independent.
4. **Cap at five surface lines.** Additional signals are available internally but not output.

---

## Suppression Conditions

FLOW does not run (W_flow = 0) when all of the following are true:
- No trigger area is touched by the change
- The change is purely mechanical (rename, reformat, move, delete)
- No operational surface is modified

FLOW runs but does not surface (W_flow < 1.5) when:
- Triggers are present but the implementation already follows the principle
- The only signals are weight-0.5 and fewer than three fire
- The drag is negligible at the expected scale

---

## Integration Specs

### FLOW → SISPIS

FLOW signals map to SISPIS entropy dimensions. When FLOW runs before SISPIS, emitted signals pre-score specific SISPIS entropy signals.

The SISPIS entropy signals are: `option_multiplicity`, `tradeoff_density`, `ambiguity_of_framing`, `comparative_intent`, `downstream_impact`.

#### Mapping Table

| FLOW signal_type | SISPIS signal affected | Delta |
|------------------|----------------------|-------|
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
| `unnecessary_caching` | `tradeoff_density` | +0.5 |
| `eager_loading` | `downstream_impact` | +0.5 |
| `repeated_computation` | `downstream_impact` | +0.5 |
| `missing_batching` | `downstream_impact` | +0.5 |
| `missing_incremental` | `tradeoff_density` | +0.5 |
| `unnecessary_abstraction` | `tradeoff_density` | +0.5 |

#### Capping

SISPIS entropy signals are scored 0-2 per signal. FLOW deltas can push a signal past 2. Cap each SISPIS signal at 2.0 after applying deltas. Overflow does not carry to adjacent signals.

#### Pipeline Operation

```
1. FLOW trigger gate: if no triggers, FLOW does not run
2. If triggers present, FLOW runs relevant principles → emits signals
3. FLOW gate: if W_flow >= 1.5, surface findings before output
4. If drag found and fixable, fix is applied before DOX closeout
5. If drag found and accepted as tradeoff, decision is recorded
6. FLOW passes emitted signal list to SISPIS
7. SISPIS applies delta table to its entropy score E
8. SISPIS runs its gate function on the resulting E
9. Output mode: NO_DECISION / EXPLANATION / SCHEMA
```

A single `retry_storm_risk` (2.0) elevates SISPIS E by +4 (two signals +2 each, capped at 2 each). This is likely to push E past 6, triggering SISPIS Stage 1 hard override → SCHEMA mode. The output must frame the retry storm risk as a decision, not just deliver the code.

### FLOW → ANCHOR

FLOW does not trigger ANCHOR's Recovery Discipline. FLOW findings are about the artifact's operational properties, not about execution failure. The integration is:

| FLOW event | ANCHOR response |
|------------|-----------------|
| FLOW surfaces a finding, fix is applied | Action Accountability records the fix (Action/Reason/Evidence/Outcome/Next State) |
| FLOW surfaces a finding, user accepts the tradeoff | Action Accountability records the accepted tradeoff and rationale |
| FLOW does not surface (W_flow < 1.5) | No ANCHOR interaction |

### FLOW → DOX

When FLOW identifies a maintenance burden that is accepted (the drag is worth it for some other benefit), ANCHOR's Action Accountability records the decision. DOX's closeout records it in the relevant AGENTS.md only if the tradeoff establishes a durable project constraint, recurring exception, or future implementation rule. When it does belong in AGENTS.md, the recorded note should include:
- The pattern that creates the drag
- The benefit that justifies accepting the drag
- The conditions under which the tradeoff should be re-evaluated

Prototype-scoped or one-off exceptions are ANCHOR-only and should not pollute AGENTS.md.

### OWL → FLOW

OWL's signals define what FLOW should evaluate. The handoff:

| OWL signal | FLOW response |
|-----------|---------------|
| `scope_expansion` | FLOW evaluates the expanded scope for operational drag |
| `over_complexity_detected` | FLOW's Maintenance Weight evaluates whether the complexity creates ongoing burden |
| `intent_deviation` | FLOW evaluates whether the deviation introduces operational patterns |
| `behavior_change_risk` | FLOW evaluates whether the behavior change affects hot paths or external I/O |

OWL does not tell FLOW what drag to find. OWL says "this is complex" or "this changes behavior"; FLOW evaluates whether that complexity or behavior change creates operational drag.

### FUSE → FLOW

FUSE and FLOW are orthogonal. FUSE governs the agent's tool use; FLOW governs the code the agent produces. They do not exchange signals through the SISPIS delta tables — each maps to SISPIS independently. However, FUSE's Evidence Interpretation can inform FLOW's evaluation: if FUSE verifies that a test passes, FLOW can evaluate whether that test covers the operational properties (load, concurrency, retry behavior) that FLOW's principles govern. This is a contextual influence, not a scored signal handoff.

### WARD → FLOW

WARD feeds accepted tradeoffs to FLOW for operational drag evaluation. The handoff:

| WARD event | FLOW response |
|------------|---------------|
| User confirms a risky action (e.g., aggressive retry loop, unbounded queue) | FLOW evaluates the confirmed pattern for operational drag. WARD owns whether it is allowed; FLOW owns whether it creates ongoing burden. |
| WARD constrains an action (e.g., adds a backup before overwrite) | FLOW evaluates whether the constraint itself introduces drag (e.g., extra I/O from backup creation in a hot path). |
| WARD refuses an action | No FLOW interaction — the action does not execute, no artifact is produced. |
| WARD allows without constraint | No FLOW interaction from WARD — FLOW evaluates the artifact on its own triggers. |

Example: WARD allows an aggressive retry loop against an external API because the user confirmed it. FLOW's Retry Discipline evaluates whether the retry loop will cascade under load. If FLOW finds `retry_storm_risk`, it surfaces — the user made an authority decision (WARD), but the operational consequence (FLOW) is still visible.

---

## Integration Test Cases

### No trigger (W_flow = 0)
Fix a typo in an error message. No triggers touched. FLOW does not run. W_flow = 0. Silent.

### Single weight-2.0 signal (W_flow = 2.0)
`retry_storm_risk` fires — a retry loop without backoff. W_flow = 2.0 >= 1.5 → surface. SISPIS receives +4 total (two signals +2 each, capped at 2 each). E likely crosses 6 → SISPIS Stage 1 hard override → SCHEMA mode.

### Two weight-1.0 signals (W_flow = 2.0)
`algorithmic_drag` + `coupling_burden`. A change adds a nested loop in a hot path that also introduces tight coupling. Surfaces. SISPIS receives +1 to `downstream_impact` and +1 to both `downstream_impact` and `tradeoff_density`. E elevates; SISPIS gate depends on base E.

### Single weight-1.0 signal (W_flow = 1.0)
`missing_timeout` fires — an API call without a timeout. W_flow = 1.0 < 1.5 → silent. SISPIS receives +1 to `downstream_impact`. The timeout is added internally without surfacing.

### Three weight-0.5 signals (W_flow = 1.5)
`unnecessary_caching` + `eager_loading` + `unnecessary_abstraction`. A change adds a cache for a cheap computation, eager-loads a config, and adds an abstraction layer. W_flow = 1.5 >= 1.5 → surfaces. SISPIS receives +0.5 to `tradeoff_density` + +0.5 to `downstream_impact` + +0.5 to `tradeoff_density` = +1.0 to `tradeoff_density`, +0.5 to `downstream_impact`.

### Trigger present, no signal (W_flow = 0)
A change adds a retry loop with exponential backoff, jitter, max retry count, timeout, and verified idempotency. Trigger present (retries). Retry Discipline evaluated. All checks pass. No signals. W_flow = 0. Silent.
