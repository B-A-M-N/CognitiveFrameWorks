# Shared Integration Contract

This file is the canonical integration contract for CognitiveFrameWorks. It resolves cross-skill drift for pipeline order, skill ownership, SISPIS signal integration, DOX policy, and generated adapter maintenance.

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

## Skill Ownership

| Skill | Owns | Does not own |
|-------|------|--------------|
| OWL | Pre-implementation reasoning, uncertainty, contradiction detection, verification criteria, reset triggers | Tool selection, authority gating, documentation contracts, operational efficiency, response structure |
| ANCHOR | Operational state continuity, checkpoints, object identity, epistemic classification, recovery procedure, completion state | Reasoning quality, tool selection, security authority, communication style |
| DOX | Documentation contracts, AGENTS.md hierarchy, closeout updates for durable contracts | Runtime reasoning, tool strategy, entropy scoring, security decisions |
| FUSE | Tool necessity, tool selection, sequencing, concurrency, bounds, evidence interpretation, retry termination | Whether an action is permitted, whether code has operational drag |
| WARD | Authority, trust boundaries, secrets, mutation consent, reversibility, supply-chain risk, policy preservation | Whether the selected tool is optimal, whether the produced code is efficient |
| FLOW | Operational drag in the produced artifact: retry storms, backpressure, cache hygiene, startup, hot paths, I/O, workflow friction, maintenance weight | Agent tool-use strategy, authority gating, documentation contracts |
| SISPIS | Output mode and response structure based on entropy, intent weight, suppression, and decision space | Reasoning, state persistence, tool execution, authority, operational efficiency |

## Activation Budget

Use the minimum subset that serves the task.

| Task type | Minimum active skills |
|-----------|-----------------------|
| Simple factual / explanatory answer | SISPIS; OWL if reasoning risk exists |
| Single-turn code advice | OWL + SISPIS |
| Multi-turn debugging | OWL + ANCHOR + FUSE + SISPIS |
| Tool-using implementation | OWL + ANCHOR + FUSE + WARD + SISPIS |
| Performance / operational drag review | OWL + FLOW + SISPIS |
| File editing in a DOX-enabled project | OWL + ANCHOR + DOX + FUSE + WARD + SISPIS; FLOW only if a trigger fires |
| Risky commands / secrets / external effects | WARD required |

Loading every skill on every request is overhead. The active subset should match the task's evidence, mutation, authority, documentation, and communication needs.

## SISPIS Integration

Upstream signal sources are OWL, ANCHOR, FUSE, FLOW, and WARD. DOX is not a SISPIS upstream source. DOX surfaces contract constraints; OWL, ANCHOR, and FUSE consume those constraints according to their own rules.

Each upstream signal consumed by SISPIS should be represented as:

```json
{
  "entropy_delta": 0,
  "intent_weight": 0.0,
  "surface": false,
  "description": "one sentence"
}
```

Deduplicate by underlying cause before applying deltas. Use the highest severity delta for a shared cause, then cap each SISPIS entropy dimension at 2.0.

### `intent_weight` Mapping

| Source | Mapping rule |
|--------|--------------|
| OWL | `intent_weight = signal weight` (`0.5`, `1.0`, or `2.0`) |
| FUSE | `intent_weight = signal weight` (`0.5` or `1.0`) |
| FLOW | `intent_weight = signal weight` (`0.5`, `1.0`, or `2.0`) |
| WARD | `proceed = 0`, `constrain = 1`, `confirm = 2`, `refuse = 2`, `recover = 2` |
| ANCHOR | Recovery event `= 1`; checkpoint/object/completion events `= 0.5`; passive state tracking `= 0` |
| DOX | `0`; DOX does not emit SISPIS entropy or intent weight |

Entropy deltas remain defined in each skill's reference files. This file defines the canonical `intent_weight` policy and deduplication contract so SISPIS can receive a stable signal shape across skills.

### Hard Output Floors

| Event | Minimum SISPIS mode |
|-------|---------------------|
| WARD `confirm` | EXPLANATION |
| WARD `refuse` | EXPLANATION |
| WARD `recover` | EXPLANATION |
| Any surfaced upstream signal with `surface = true` | EXPLANATION |
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

## Validation

Run:

```bash
python3 scripts/validate-framework.py
```

The validator checks JSON syntax, expected skill count, adapter generation drift, WARD `recover` mapping, and canonical integration drift.
