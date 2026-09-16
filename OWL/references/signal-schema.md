# OWL Signal Schema — Integration Spec

## Signal Type Registry

Complete registry of all signal types, organized by principle. Multiple types per principle — a principle can emit any signal from its list in a single reasoning pass.

### Epistemics

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `unverified_assumption` | 1.0 | Approach depends on an assumption that hasn't been verified and is not inferable from available context |
| `ambiguous_requirement` | 0.5 | The request has two or more distinct interpretations with meaningfully different implementations |
| `multiple_interpretations` | 0.5 | More than two interpretations exist; presenting them requires a decision from the user |
| `user_observation_conflict` | 2.0 | The user reports current observable behavior that conflicts with a prior agent conclusion, completion claim, or verified state — reinspection is required |
| `position_pressure` | 1.0 | User disagreement arrived without new information — social pressure to revise a correct analysis |

### Reality

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `code_not_read` | 1.0 | Implementation depends on code contents but the code has not been read |
| `contradiction` | 2.0 | Code behavior directly contradicts what the request implies — different API, wrong type, already implemented, missing dependency |
| `missing_context` | 0.5 | Context needed to verify the approach is not available and cannot be safely inferred |
| `constraint_drift` | 1.0 | In a long task, a constraint stated early may no longer be reflected in current direction |

### Verification

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `missing_criteria` | 1.0 | Success is not derivable from the request — "fix it" or "make it work" without a testable state |
| `unverifiable_claim` | 1.0 | A claim is being made that cannot be verified with available tools, context, or code |
| `partial_completion` | 1.0 | Some part of the task is complete but another part cannot be verified — labeling required |
| `circular_verification` | 2.0 | A completion claim materially rests on edit existence, an implementation-mirroring test, an unexercised failing boundary, rereading newly written code, or a prior agent message |

### Locality

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `scope_expansion` | 1.0 | Completing the task requires modifying things beyond what the request implied |
| `unrelated_change_detected` | 0.5 | A change in scope that doesn't trace to the user's request was about to be made |

### Conservation

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `intent_deviation` | 2.0 | The implementation would change observable behavior beyond what was requested |
| `behavior_change_risk` | 1.0 | A meaningful probability exists that existing behavior breaks, even if not certain |

### Simplicity

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `over_complexity_detected` | 0.5 | Current approach is detectably more complex than the problem requires |
| `cohesion_risk` | 2.0 | A proposed edit adds another distinct responsibility to an already multi-responsibility unit — responsibility ownership is becoming entangled |

### Generalization

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `abstraction_added` | 0.5 | An abstraction, helper, or pattern is being added beyond the literal request |
| `premature_pattern` | 0.5 | A pattern is being introduced where a specific, inline solution would be sufficient |

### Debuggability

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `opacity_risk` | 0.5 | An implementation choice introduces behavior that would surprise a reader or make future debugging significantly harder |

### Integrity

| Signal Type | Weight | Condition |
|-------------|--------|-----------|
| `approach_failed` | 2.0 | The current approach has demonstrably failed and continuing it is a sunk cost |
| `simulated_completion_risk` | 2.0 | There is a risk of presenting inferred or fabricated content as verified output |
| `sunk_cost_detected` | 1.0 | Resistance to abandoning a wrong path because of prior work invested in it |

---

## Weight Summary

| Weight | Signal Types |
|--------|-------------|
| 2.0 | `contradiction`, `intent_deviation`, `approach_failed`, `simulated_completion_risk`, `user_observation_conflict`, `circular_verification`, `cohesion_risk` |
| 1.0 | `unverified_assumption`, `position_pressure`, `code_not_read`, `constraint_drift`, `missing_criteria`, `unverifiable_claim`, `partial_completion`, `scope_expansion`, `behavior_change_risk`, `sunk_cost_detected` |
| 0.5 | `ambiguous_requirement`, `multiple_interpretations`, `missing_context`, `unrelated_change_detected`, `over_complexity_detected`, `abstraction_added`, `premature_pattern`, `opacity_risk` |

---

## Gate Threshold

```
W_owl = sum of all emitted signal weights

W_owl >= 1.5  →  Surface mode
W_owl <  1.5  →  Silent mode
```

A single weight-2.0 signal always triggers surfacing. Two weight-1.0 signals trigger surfacing. Three weight-0.5 signals do trigger (sum = 1.5; threshold is >= 1.5). Two weight-0.5 signals do not (sum = 1.0).

---

## Stacking Rules

Multiple signals can surface in a single response. Rules for stacking:

1. **Order by descending weight.** Higher-weight signals appear first — they have higher action consequence.
2. **Suppress redundant signals from the same principle.** If `contradiction` already surfaces, do not also surface `code_not_read` from the same observation — the contradiction implies the code was read.
3. **Don't merge finding + implication across signals.** Each signal line is independent. Merging them obscures which principle fired for which reason.
4. **Cap at five surface lines.** If W_owl is very high (e.g., multiple weight-2.0 signals), the first five by descending weight are surfaced. Additional signals are available internally but not output — the goal is to change what the user does, not to enumerate every finding.

---

## Conflict Resolution

If two signals contradict each other (rare but possible):

1. Higher-weight signal takes precedence.
2. If equal weight, prefer the signal from the earlier principle in the SKILL.md ordering (Epistemics > Reality > Verification > Locality > Conservation > Simplicity > Generalization > Debuggability > Integrity).
3. Both can surface if they represent genuinely distinct findings — conflicting signals usually indicate a design ambiguity that should be surfaced anyway.

---

## Suppression Conditions

Do not run the reasoning pass (W_owl = 0, silent) when all of the following are true:
- Task is purely mechanical: rename, reformat, move, delete
- No code read is required to complete it
- The request is unambiguous (single valid interpretation)
- No existing code is being modified in a way that could change behavior

Examples: "rename `usr` to `user` in this file", "add a blank line between these two functions", "move this function to the bottom of the file."

---

## SISPIS Integration

OWL emits **semantic signals** to SISPIS in the canonical envelope
(`shared/signal.schema.json`): `signal_id`, `cause_id`, `source`,
`signal_type`, `severity`, `scope`, `evidence_refs`, `required_action`.
OWL does not compute SISPIS entropy, intent weight, or output floors — SISPIS
owns every signal → calibration mapping (see `shared/integration.md`
§ SISPIS Integration).
### Capping


### Pipeline Operation

```
1. OWL runs reasoning pass → emits semantic signals
2. OWL gate: if W_owl >= 1.5, surface findings before output
3. OWL passes emitted signals to SISPIS in the canonical envelope
4. SISPIS owns signal → entropy / intent / output-floor calibration
   (SISPIS/references/signal-calibration.yaml — the only calibration table)
5. SISPIS runs its gate function (SISPIS/runtime/calibrate.py when present)
6. Output mode: NO_DECISION / EXPLANATION / SCHEMA
```

Whether an OWL signal changes SISPIS output structure is SISPIS's decision —
OWL never computes SISPIS entropy or intent deltas.

---

## Integration Test Cases

### Low-signal request (W_owl = 0)
Rename a variable. No signals. SISPIS receives E unelevated. If the request was already low-entropy, output is direct.

### Single weight-1.0 signal (W_owl = 1.0)
`missing_criteria` fires. W_owl = 1.0 < 1.5 → silent. The emitted envelope
(owl-level semantics only) is passed to SISPIS; whether it changes response
structure is SISPIS calibration's decision.

### Single weight-2.0 signal (W_owl = 2.0)
`contradiction` fires. W_owl = 2.0 >= 1.5 → surface. The envelope crosses to
SISPIS with owl semantics; SISPIS applies its own calibration.

### Two weight-1.0 signals (W_owl = 2.0)
`unverified_assumption` + `scope_expansion`. Surfaces. Envelopes cross to
SISPIS; SISPIS calibrates, OWL does not.
