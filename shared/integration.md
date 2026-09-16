# Shared Integration Contract

This file is the canonical integration contract for CognitiveFrameWorks. It resolves cross-skill drift for pipeline order, skill ownership, SISPIS signal integration, DOX policy, and generated adapter maintenance.

## Universal Behavioral Kernel (always-on)

The compressed always-on rule set. Everything else loads dynamically; this is
the only behavior guaranteed present in every runtime bundle:

> Current external observation invalidates stale conflicting conclusions.
> Agent/tool/action success is not outcome evidence.
> Completion requires evidence at the claimed boundary.
> Repeated failure requires a changed hypothesis rather than repeated retries.
> Respect authority/ownership boundaries and do not add unrelated
> responsibility to an already broad owner merely because it is locally
> convenient.

This kernel is what the guard resolver treats as always-on (see
DigitalPsychology `feedback_loop.py` `GuardCompiler.always_on_ids`); the
per-task guard pack is layered on top of it. The distinction is maximum
**intelligence available** vs. maximum **instructions simultaneously active**
— runtime agents should see less, not more, as the system gets more
sophisticated.

## Canonical Pipeline

When used together:

```text
Request
  → OWL preflight
  → ANCHOR state baseline
  → DOX load, only when editing in a DOX-enabled project
  → FUSE + WARD wrap every action/tool decision
  → artifact/edit produced
  → FLOW evaluates the artifact only if a trigger fires
  → DOX closeout only when durable documentation contracts changed
  → ANCHOR checkpoint/reclassification if needed
  → SISPIS final output
  → Output
```

FUSE + WARD are not a linear stage. They wrap each action/tool decision. FLOW runs once per artifact, not per tool call. DOX load happens before edits; DOX closeout happens after FLOW only when documentation contracts need updating.

## User Contradiction Path

A user report of current observable behavior is evidence, not social pressure.

```text
user-reported observation
    -> OWL Reality/Epistemics: user_observation_conflict
    -> ANCHOR: prior Verified/Resolved state downgraded/reopened
    -> FUSE: fresh evidence required; stale evidence cannot satisfy Necessity
    -> execution/reinspection
    -> ANCHOR: reclassify based on fresh evidence
```

The report does not prove the user's proposed root cause. Unsupported disagreement still triggers one fresh check of load-bearing evidence before a conclusion is retained. The agent's prior conclusion has no privilege merely because the agent authored it.

## Structural Concentration Path

Responsibility cohesion is evaluated before and after the edit.

```text
proposed change
    -> OWL: cohesion_risk before edit
    -> minimal responsibility-boundary correction
    -> implementation
    -> FLOW: responsibility_concentration/change_amplification postflight
    -> fix before DOX closeout
```

Conflict resolution: OWL Locality cannot suppress a decomposition required to prevent responsibility concentration; OWL Generalization cannot classify responsibility extraction as premature solely because there is one caller. Conservation governs extraction—preserve behavior while moving ownership, then implement the new behavior through that boundary.

## Skill Ownership

| Skill | Owns | Does not own |
|-------|------|--------------|
| OWL | Pre-implementation reasoning, uncertainty, user-observation conflicts, responsibility cohesion risk, verification criteria, reset triggers | Tool selection, authority gating, documentation contracts, operational efficiency, response structure |
| ANCHOR | Operational state continuity, checkpoints, object identity, epistemic classification, reopen lifecycle, recovery procedure, completion state | Reasoning quality, tool selection, security authority, communication style |
| DOX | Documentation contracts, AGENTS.md hierarchy, closeout updates for durable contracts | Runtime reasoning, tool strategy, entropy scoring, security decisions |
| FUSE | Tool necessity under conflicting evidence, tool selection, sequencing, concurrency, bounds, stale/circular evidence rejection, retry termination | Whether an action is permitted, whether code has operational drag |
| WARD | Authority, trust boundaries, secrets, mutation consent, reversibility, supply-chain risk, policy preservation | Whether the selected tool is optimal, whether the produced code is efficient |
| FLOW | Operational drag in the produced artifact: retry storms, backpressure, cache hygiene, startup, hot paths, I/O, workflow friction, responsibility concentration, maintenance weight | Agent tool-use strategy, authority gating, documentation contracts |
| SISPIS | Output mode and response structure based on entropy, intent weight, suppression, and decision space | Reasoning, state persistence, tool execution, authority, operational efficiency |

## Activation Profiles

Use the minimum subset that serves the task. Activation profiles are owned by
`shared/pipeline.yaml` — this document renders them, and `resolve-runtime.py`
reads them. `anchor` and `dox` are aliases for the concrete stages
`anchor_open`/`anchor_closeout` and `dox_load`/`dox_closeout`.

| Profile | Kernel |
|---------|--------|
| quick | sispis |
| advice | owl, sispis |
| debug | owl, anchor, fuse, sispis |
| implement | owl, anchor, fuse, ward, sispis |
| performance | owl, flow, sispis |
| doc_edit | owl, anchor, dox, fuse, ward, sispis (FLOW only if a trigger fires) |

Risky commands / secrets / external effects always require WARD, whatever
else loads.

## Dependency vs Ordering

`shared/pipeline.yaml` separates **hard dependencies** from **ordering**:

- `requires`: the stage cannot run unless these stages are active. Closure
  over `requires` defines the minimal legal kernel (validator-enforced for
  every profile).
- `order_after`: if both are active, these run before it. `order_after` is
  not a dependency — a profile may legally omit ordered stages.

Lifecycle semantics are explicit in the manifest: `execution_mode`
(preflight/wrapper/postflight/state/pre_edit/output) and `frequency`
(per_task/per_action/per_artifact/per_edit/per_response). For example, FUSE
and WARD wrap each action; FLOW runs once per artifact only when a trigger
fires; DOX load runs before editing whenever an AGENTS contract applies, and
DOX closeout runs after the edit only when durable documentation changed.

Loading every skill on every request is overhead. The active subset should match the task's evidence, mutation, authority, documentation, and communication needs.

## SISPIS Integration

Upstream signal sources are OWL, ANCHOR, FUSE, FLOW, and WARD. DOX is not a SISPIS upstream source. DOX surfaces
contract constraints; OWL, ANCHOR, and FUSE consume those constraints
according to their own rules.

Upstream protocols emit **semantic signals only**. They do not compute SISPIS
entropy, intent weight, or output floors. SISPIS alone owns every mapping from
a signal to response structure:

```text
signal → entropy
signal → intent weighting
signal → output floor
```

Each upstream signal crossing a skill boundary is carried in the canonical
envelope in `shared/signal.schema.json`:

```json
{
  "signal_id": "sig-123",
  "cause_id": "cause-42",
  "source": "fuse",
  "signal_type": "fuse.overclaimed_evidence",
  "severity": "medium",
  "scope": "artifact",
  "evidence_refs": ["test-31", "claim-9"],
  "required_action": "reclassify"
}
```

SISPIS deduplicates by `cause_id` before applying its own calibration — one
cause may emit many signals, but it adjusts response structure once. The
envelope fields are defined in `shared/signals.md`; the envelope schema is
`shared/signal.schema.json`.

Hard output floors are SISPIS-owned calibration, stated here only as the
cross-skill expectation:

| Event | Minimum SISPIS mode |
|-------|---------------------|
| WARD `confirm` | EXPLANATION |
| WARD `refuse` | EXPLANATION |
| WARD `recover` | EXPLANATION |
| User explicitly requests options / deep dive | Gate activation |
| User explicitly requests simple / direct answer | Suppression unless safety or hard override requires otherwise |

## DOX Policy

DOX preserves contracts; it does not expand task scope.

DOX closeout updates AGENTS.md only when a change affects durable project contracts, ownership, scope, workflow, permissions, recurring operating rules, or child index structure. One-off FLOW tradeoffs remain in ANCHOR unless they establish a durable project constraint, recurring exception, or future implementation rule.

DOX does not create `FLOW_ISSUES.md` unless the user explicitly asks for durable issue tracking. If FLOW finds operational drag but the tradeoff is accepted and one-off, ANCHOR records the decision and rationale.

## Recovery Handoff

Recovery is owned by ANCHOR.

| Trigger source | Finding owner | Recovery owner |
|----------------|---------------|----------------|
| OWL `approach_failed` / `sunk_cost_detected` | OWL | ANCHOR |
| FUSE `retry_bound_exceeded` | FUSE | ANCHOR |
| WARD `refuse` / `recover` | WARD | ANCHOR |

Surface one merged block, not separate duplicate blocks.

## Adapter Generation

Adapter files under `*/adapters/` are generated artifacts. They are derived from each skill's canonical `adapters/CLAUDE.md` using `shared/adapter-source.md` and `scripts/generate-adapters.py`.

Do not hand-edit generated adapter variants. Edit the canonical adapter content, then regenerate.

Adapters derive their pipeline/integration summaries from the canonical
manifest `shared/pipeline.yaml` and the canonical envelope
`shared/signal.schema.json`. `shared/signals.md` is the canonical signal
registry; `shared/signal-registry.json` is its machine-readable form. Keep
these files canonical; edit derived prose, not this contract, when stages
change.

## Validation

Run:

```bash
python3 scripts/validate-framework.py
python3 scripts/doctor.py
python3 scripts/resolve-runtime.py <task> <shape> <domains>
```

`validate-framework.py` checks skill frontmatter loadability (every
`*/SKILL.md` and `cogframe/SKILL.md` must parse as YAML), canonical signal
envelope and registry agreement, absence of upstream SISPIS math, pipeline
manifest shape, StateWork manifest schema conformance, WARD `recover`
mapping, adapter generation drift, and shared integration drift.
`doctor.py` verifies the installed runtime registry: source validity, runtime
registration, installed/source fingerprint match, dispatcher target
resolvability, required reference files, and schema availability.
`resolve-runtime.py` composes the runtime bundle for a task: pinned
framework/statework/guard versions, minimum FrameWorks kernel, best-matching
StateWork from the registry, and the validated guard pack — with telemetry
emitted out of band.
