---
name: cogframe
description: CognitiveFrameWorks pipeline dispatcher — routes a task to the minimal subset of the seven behavioral protocols (owl, anchor, dox, fuse, flow, ward, sispis) and enforces their execution order. Use at the start of any non-trivial engineering task - multi-step implementation, debugging session, refactor, tool-heavy work, or risky operations - to decide which protocols apply. Do not use for trivial one-line answers.
---

# cogframe — CognitiveFrameWorks Dispatcher

The seven protocols are designed as a pipeline, but skills trigger
individually. This dispatcher is the pipeline: consult it once at task
start, load ONLY the subset the task needs, and apply them in order.

cogframe does NOT hard-code the vocabulary of any domain. It inspects the
StateWork registry and activates a compatible composition of domain StateWorks
(zero or more roots, plus required dependencies and flow-level specialists).
StateWorks are
**discovered/routed by manifests and exposed to supported runtimes through
generated skill wrappers** — this file stays small as the registry grows.

## Dispatch

For every non-trivial task:

```text
1. Determine execution shape (quick answer / code advice / debugging /
   tool-using implementation / performance review / doc-constrained edit).
2. Load the minimum FrameWorks kernel for that shape.
3. Query the StateWork registry (see below).
4. Activate the best-matching StateWork composition and its minimal specialists.
5. Route typed handoff packets between StateWorks when the domain changes.
```

Activation profiles live in `shared/pipeline.yaml` (single source of
truth). Resolve the active profile from `shared/pipeline.yaml`; do not
maintain another table here. Run `scripts/resolve-runtime.py` (or call
`scripts/resolve_runtime.resolve()` / `scripts/runtime.start()` directly) to
compose the kernel for a profile, and `python3 scripts/runtime.py <task>
<shape> <domains>` for a debugging front end.

`anchor` and `dox` expand through the manifest's aliases to
`anchor_open`/`anchor_closeout` and `dox_load`/`dox_closeout`. The kernel is
ordered by the manifest's `order_after` edges at runtime; `requires` are the
only hard dependencies (resolver performs the transitive closure). Destructive
commands, secrets, network mutations always add `ward` (mandatory, whatever
else loads).

## StateWork Registry

StateWorks live in `~/CognitiveStateWork/` and are exposed by their
manifest (for example `infrae/manifest.yaml`). cogframe reads the registry,
never the routing prose. A manifest-entry shape is:

```yaml
id: infrae
domain: infrastructure_state
purpose: Model, change, and verify infrastructure state with current evidence.
name: Infrae
entrypoint: infrae/STATEWORK.md
advisory_entrypoint: SKILL.md
triggers:
  - infrastructure
  - topology
  - capacity
  - deployment
  - incident
phase_tags: [domain_state]
inputs:
  required: [task_context]
  optional: []
emits:
  - infrastructure_truth_packet
  - infrastructure_evidence_packet
context_budget: medium
composition:
  group: infrastructure_state
  mode: exclusive
  requires: []
  conflicts: []
integrations:
  cognitiveframeworks:
    required: [owl]
    enhancements: [anchor]
flow_selection:
  fallback: optional
  ambiguity: error
  no_match: inactive
```

Dispatch rule: select only compatible StateWorks and activate at most one per
exclusive group. Load only each selected entrypoint, active flow, and at most
two relevant specialists. If a StateWork's handoff packet is already present
and current, prefer it over re-observation.

## Composition

StateWorks never call each other. The composition layer resolves dependencies,
conflicts, and typed packet inputs before activating compatible workflows. When
a task crosses domains, the producing StateWork emits a typed handoff packet;
the composition layer routes it to the consumer that declares the input.

## Execution Order

For whatever subset is loaded, apply in this order — later stages consume
earlier stages' findings, never the reverse:

```text
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
