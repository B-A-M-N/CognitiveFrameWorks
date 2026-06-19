# FLOW Cheatsheet — Quick Reference

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

## Anti-Goal

FLOW does not optimize for cleverness, micro-performance, or theoretical elegance.
FLOW only acts when implementation choices create measurable or likely operational drag.

## Gate

```
No triggers → W_flow = 0, FLOW does not run
Triggers present:
  W_flow = sum of emitted signal weights
  W_flow >= 1.5  →  Surface mode
  W_flow <  1.5  →  Silent mode
```

## 8 Principles (one line + pressure clause)

| # | Principle | Default | Under Pressure |
|---|-----------|---------|----------------|
| 1 | **Retry Discipline** | Retries need backoff, jitter, bounds. Non-idempotent ops don't retry blindly. | Don't skip backoff "to keep it simple." Retry storms are worse than no retry. |
| 2 | **Backpressure** | Queues/streams need bounds. Producers respect consumer capacity. | Don't add unbounded buffers "for safety." Unbounded accumulation is a memory leak. |
| 3 | **Cache Hygiene** | Caching needs invalidation strategy. Complexity proportional to benefit. | Don't cache everything "for speed." Unnecessary caching adds maintenance cost. |
| 4 | **Startup Efficiency** | Startup is fast. Expensive work deferred or lazy. | Don't eager-load "to be safe." Blocking startup affects every cold start. |
| 5 | **Hot-Path Awareness** | Hot paths avoid algorithmic drag. Loops efficient at expected scale. | Don't ignore O(n²) "because n is small." n grows. |
| 6 | **External I/O Discipline** | External calls batched, paginated, non-blocking where volume warrants. | Don't fetch one-by-one "because it's simpler." N+1 is a classic for a reason. |
| 7 | **Workflow Friction** | Change doesn't slow build/test/dev cycle. Feedback loops stay fast. | Don't add synchronous steps "because it's correct." Slow CI is operational drag. |
| 8 | **Maintenance Weight** | Abstractions reduce more complexity than they add. Maintenance proportional to benefit. | Don't add abstractions "for flexibility." Unused flexibility is maintenance cost. |

## Signal Weights

| Weight | Signal Types |
|--------|-------------|
| 2.0 | `retry_storm_risk`, `unbounded_accumulation`, `n_plus_one_query`, `cache_stampede_risk` |
| 1.0 | `non_idempotent_retry`, `missing_timeout`, `missing_flow_control`, `stale_cache_risk`, `blocking_startup`, `algorithmic_drag`, `missing_pagination`, `sync_blocking_io`, `workflow_friction`, `coupling_burden` |
| 0.5 | `unnecessary_caching`, `eager_loading`, `repeated_computation`, `missing_batching`, `missing_incremental`, `unnecessary_abstraction` |

## Signal Types by Principle

| Principle | Signal Types |
|-----------|-------------|
| Retry Discipline | `retry_storm_risk`, `non_idempotent_retry`, `missing_timeout` |
| Backpressure | `unbounded_accumulation`, `missing_flow_control` |
| Cache Hygiene | `cache_stampede_risk`, `stale_cache_risk`, `unnecessary_caching` |
| Startup Efficiency | `blocking_startup`, `eager_loading` |
| Hot-Path Awareness | `algorithmic_drag`, `repeated_computation` |
| External I/O Discipline | `n_plus_one_query`, `missing_pagination`, `sync_blocking_io`, `missing_batching` |
| Workflow Friction | `workflow_friction`, `missing_incremental` |
| Maintenance Weight | `coupling_burden`, `unnecessary_abstraction` |

## Surface Format

```
**[Principle]:** [finding — what was observed]. [implication — what drag this creates.]

[artifact with drag addressed, or explicit tradeoff noted]
```

Multiple signals: stack by descending weight, cap at 5 lines.

## When Not to Surface

- No trigger area touched → FLOW does not run
- Finding is theoretical improvement, not operational drag
- Drag is negligible at expected scale
- Tradeoff was explicitly accepted by user
- W_flow < 1.5

## Decision Quick-Map

| Question | Principle | Action |
|----------|-----------|--------|
| Does retry have backoff + jitter + max? | Retry Discipline | No backoff → `retry_storm_risk`. No max → `missing_timeout`. Non-idempotent → `non_idempotent_retry`. |
| Is the queue bounded? | Backpressure | No limit → `unbounded_accumulation`. No flow control → `missing_flow_control`. |
| Does cache have invalidation? | Cache Hygiene | No invalidation → `stale_cache_risk`. No stampede guard → `cache_stampede_risk`. Cheap computation cached → `unnecessary_caching`. |
| Is startup fast? | Startup Efficiency | Expensive sync work in init → `blocking_startup`. Eager load where lazy works → `eager_loading`. |
| Is the hot path efficient? | Hot-Path Awareness | O(n²) where O(n) suffices → `algorithmic_drag`. Repeated work in loop → `repeated_computation`. |
| Are external calls batched/paginated? | External I/O Discipline | Query in loop → `n_plus_one_query`. No limit on fetch → `missing_pagination`. Blocking I/O on stall path → `sync_blocking_io`. |
| Does this slow the dev cycle? | Workflow Friction | Slower build/test/CI → `workflow_friction`. Breaks incremental → `missing_incremental`. |
| Does the abstraction earn its cost? | Maintenance Weight | Adds coupling → `coupling_burden`. No complexity reduction → `unnecessary_abstraction`. |

## Integration Points

### FLOW → SISPIS

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
| `unnecessary_caching` | `tradeoff_density` | +0.5 |
| `eager_loading` | `downstream_impact` | +0.5 |
| `repeated_computation` | `downstream_impact` | +0.5 |
| `missing_batching` | `downstream_impact` | +0.5 |
| `missing_incremental` | `tradeoff_density` | +0.5 |
| `unnecessary_abstraction` | `tradeoff_density` | +0.5 |

Apply before Stage 1. Cap each SISPIS signal at 2.0.

### FLOW → ANCHOR

FLOW does not trigger Recovery Discipline (that's FUSE/OWL's domain). When FLOW surfaces a finding and the user accepts the tradeoff, ANCHOR's Action Accountability records the decision and rationale.

### FLOW → DOX

When FLOW identifies an accepted maintenance burden, DOX's closeout pass records the decision in the relevant AGENTS.md.

## Pipeline Position

```
Request → OWL → ANCHOR → DOX(load) → FUSE → Edit → FLOW → DOX(closeout) → SISPIS → Output
```

FLOW runs once per artifact, evaluating the produced code for operational drag.

## Trigger Check Quick List

Before running FLOW, check if the task touches any trigger:
1. Retry/backoff/timeout code? → Yes → run Retry Discipline
2. Queue/stream/buffer/concurrency? → Yes → run Backpressure
3. Cache/memoization/TTL? → Yes → run Cache Hygiene
4. Module load/app startup/init? → Yes → run Startup Efficiency
5. Loops/scans/hot paths? → Yes → run Hot-Path Awareness
6. Provider/API calls? → Yes → run External I/O Discipline
7. Database/filesystem access? → Yes → run External I/O Discipline, Hot-Path Awareness
8. Build/test/CI/dev workflow? → Yes → run Workflow Friction
9. New abstraction/pattern/coupling? → Yes → run Maintenance Weight
10. Long-lived maintenance burden (duplicated config, hardcoded values spread across modules, coupling forcing coordinated edits)? → Yes → run Maintenance Weight

All No → FLOW does not run.
