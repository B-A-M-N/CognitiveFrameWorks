# SISPIS Integration — End-to-End Signal Pipeline

Calibrates: OWL semantic signal output → SISPIS calibration ownership, full
pipeline, two scenarios.

## How the Pipeline Works

```
Request
  → OWL reasoning pass
      emits: semantic signals in the canonical envelope
             (shared/signal.schema.json)
  → OWL gate: if W_owl >= 1.5, surface findings before output
  → OWL passes the signal list to SISPIS
      SISPIS owns: signal → entropy, signal → intent weighting,
                   signal → output floor
  → SISPIS gate runs
  → Output mode: NO_DECISION / EXPLANATION / SCHEMA
```

OWL does not compute SISPIS entropy, deltas, or output floors. It emits
meaning — *what was found* — and SISPIS decides *how that shapes response
structure*. The two skills share the envelope, never a delta table. This is
canonical in `shared/integration.md` § SISPIS Integration.

## Scenario 1: High-Stakes Question

### Request

> Should I migrate the authentication system to OAuth2?

### OWL reasoning pass

**Epistemics:** "Migrate authentication" has multiple interpretations —
migrate all users, migrate new users only, run parallel systems, migrate
partially. These have materially different implementation paths and costs.

Signal: `multiple_interpretations` (0.5) — semantic meaning: the decision
space is broader than the surface question.

**Reality:** No current auth implementation was provided.

Signal: `missing_context` (0.5) — semantic meaning: scope depends on state
not yet observed.

**Verification:** Success criteria for "migrate" is unclear.

Signal: `missing_criteria` (1.0) — semantic meaning: completion is undefined.

W_owl = 2.0 >= 1.5 → Surface.

### The handoff

OWL emits an envelope per signal:

```json
{
  "signal_id": "sig-owl-01",
  "cause_id": "cause-auth-scope",
  "source": "owl",
  "signal_type": "owl.multiple_interpretations",
  "severity": "medium",
  "scope": "task",
  "evidence_refs": ["request-text"],
  "required_action": "clarify_before_deciding"
}
```

SISPIS receives the envelope and owns calibration — whether these meanings
raise entropy, weight intent, or force a structure floor is SISPIS's
decision. The request already looks decisional ("should I"); SISPIS may
activate on its own, and the OWL signals tell it *which dimension of ambiguity
the structure must address* ("scope of migration is unclear").

## Scenario 2: Hidden Complexity

### Request

> Add error logging to the payment handler.

### OWL reasoning pass

**Reality:** Code is read. The payment handler has try/catch that swallows
exceptions silently — no logging, no re-raise, no error propagation. Adding
error logging to a silent-catch pattern logs the error but still hides it from
callers.

Signal: `contradiction` (2.0) — semantic meaning: the request assumes a code
state that does not exist.

**Conservation:** Adding logging without fixing the swallow changes the
observability without changing the behavior.

Signal: `intent_deviation` (2.0) — semantic meaning: "add logging" could mean
(a) add logging and leave swallow behavior, or (b) add logging and fix the
swallow.

W_owl = 4.0 >= 1.5 → Surface.

### The handoff

```json
{
  "signal_id": "sig-owl-02",
  "cause_id": "cause-silent-catch",
  "source": "owl",
  "signal_type": "owl.contradiction",
  "severity": "high",
  "scope": "artifact",
  "evidence_refs": ["payment-handler.ts", "try-catch-block"],
  "required_action": "reclassify_request_assumption"
}
```

SISPIS owns what this means for output structure. The semantic content — the
user is not simply choosing whether to add logging, but choosing between two
materially different implementations, one of which silently breaks error
handling — is what SISPIS weighs.

## Integration Summary

| Scenario | OWL semantic finding | Handoff |
|----------|----------------------|---------|
| "Should I migrate auth?" | decision space broader than surface question; scope undefined | `owl.multiple_interpretations`, `owl.missing_criteria` → SISPIS |
| "Add error logging" | request assumes a code state that does not exist; two divergent implementations | `owl.contradiction`, `owl.intent_deviation` → SISPIS |

OWL's most valuable integration role is Scenario 2: catching cases where the
request looks simple but the code reveals complexity that changes the decision
space — and handing that finding to SISPIS as meaning, not math.
