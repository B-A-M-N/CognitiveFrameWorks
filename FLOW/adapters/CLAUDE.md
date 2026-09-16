<!-- Generated from shared/adapter-source.md. Do not edit directly. -->

<!-- Generated from the compact runtime capsule (FLOW/runtime.md). Do not edit directly. -->

# FLOW — Runtime Adapter (compact)

> Installed skills load this compact kernel; full authoring material lives in
> `references/`, `examples/`, and the source `SKILL.md`. Calling the full
> protocol adds `~155` words of active surface, not thousands.

# FLOW — Runtime Capsule

Post-flight operational drag on produced artifacts. Activates only when a
trigger fires; otherwise silent.

## Trigger gate (1-2 sentences)
Does the produced artifact introduce retry/backpressure risk, cache
staleness, startup cost, hot-path cost, external I/O without bounds,
workflow friction, central-object growth, or maintenance weight?
- No → stop (FLOW does nothing).
- Yes → run the analysis below once per artifact.

## Analysis
- responsibility_concentration / change_amplification: central objects and
  growing responsibility after the edit
- retry storms / backpressure: unbounded retries, no backoff, queue growth
- cache hygiene: stale caches, missing invalidation
- startup / hot path: expensive work on cold start or in loops
- external I/O: un-bounded calls, missing timeouts
- workflow friction / maintenance weight: operational and upkeep burden

## Signals
retry_storm, missing_backpressure, stale_cache, startup_cost, hot_path_cost,
unbounded_io, missing_timeout, unnecessary_caching, eager_loading,
unnecessary_abstraction, coupling_burden, responsibility_concentration,
change_amplification, algorithmic_drag

Emit in the canonical envelope only (shared/signal.schema.json); never
compute SISPIS scoring here.
