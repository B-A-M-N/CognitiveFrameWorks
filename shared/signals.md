# Shared Signal Registry

Cross-skill signal primitives. These signals are shared vocabulary understood
by multiple skills. Each skill may also define its own local signal details in
its reference files.

## Signal Types

| Signal | Description | Emitted by | Consumed by |
|--------|-------------|------------|-------------|
| `owl.verification_needed` | A claim requires verification before it can be treated as established | OWL | FUSE, SISPIS |
| `owl.approach_failed` | Current reasoning or implementation approach is not working | OWL | ANCHOR, SISPIS |
| `owl.constraint_drift` | The constraints of the task have shifted from what was originally established | OWL | ANCHOR, SISPIS |
| `owl.user_observation_conflict` | A user report of current observable behavior conflicts with a prior agent conclusion, completion claim, or verified state | OWL | ANCHOR, FUSE, SISPIS |
| `owl.circular_verification` | A completion claim materially rests on edit existence, an implementation-mirroring test, rereading newly written code, or a prior agent message | OWL | ANCHOR, SISPIS |
| `owl.cohesion_risk` | A proposed edit adds another distinct responsibility to an already multi-responsibility unit | OWL | FLOW, SISPIS |
| `fuse.retry_bound_exceeded` | Tool execution has exceeded retry bounds without success | FUSE | ANCHOR, SISPIS |
| `fuse.overclaimed_evidence` | A tool result is being interpreted more broadly than it warrants | FUSE | ANCHOR, SISPIS |
| `fuse.conflicting_evidence_unrechecked` | An earlier observation or passing test is relied on after a newer contradictory report without fresh inspection | FUSE | ANCHOR, SISPIS |
| `flow.responsibility_concentration` | The produced artifact concentrates responsibility in one owner beyond its scope | FLOW | SISPIS, ANCHOR |
| `flow.operational_drag` | The produced code creates measurable operational friction or load | FLOW | SISPIS, ANCHOR |
| `flow.change_amplification` | The produced artifact amplifies the impact of a small change across a wide surface | FLOW | SISPIS, ANCHOR |
| `ward.authority_risk` | An action carries authority, permission, or scope risk | WARD | ANCHOR, SISPIS |
| `ward.secret_exposure` | An action would expose or transmit secrets | WARD | ANCHOR, SISPIS |
| `anchor.recovery_started` | ANCHOR has initiated a recovery procedure | ANCHOR | OWL, FUSE, SISPIS |
| `ward.confirmation_required` | An action requires user confirmation before proceeding | WARD | SISPIS |

The machine-readable form is `shared/signal-registry.json`, which carries the
same `consumers` list. A signal appears here only when another component
actually consumes it; purely local diagnostics stay in the producing skill's
own reference files.

## Scope

This registry contains only cross-skill primitives — signals that are
transmitted between skills. Skill-specific internal signals (e.g., FLOW's
`n_plus_one_query`, WARD's `untrusted_execution`) are maintained in each
skill's own reference files and are not listed here.

## Canonical IDs

Every signal that crosses a skill boundary must carry a namespaced,
versionless, stable identifier matching the producer's own registry entry.
Namespaced IDs are canonical; prose aliases may exist only as pointers, never
as first-class registry rows. For example, the FUSE Evidence Interpretation
signal that fires when a tool result is interpreted more broadly than it
warrants — `overclaimed_evidence` in FUSE's registry — is canonical as
`fuse.overclaimed_evidence`. The legacy spelling `evidence_overclaim` is a
recognized variant and is not a valid registry ID.

## Canonical Signal Envelope

Any signal crossing a skill boundary is carried in the canonical envelope
defined in `shared/signal.schema.json`. The envelope fields are:

| Field | Meaning |
|-------|---------|
| `signal_id` | Stable unique identifier for this emission instance |
| `cause_id` | Identifier of the underlying cause, shared across deduplicated emissions |
| `source` | Emitting skill, e.g. `fuse` |
| `signal_type` | Namespaced type, e.g. `fuse.overclaimed_evidence` |
| `severity` | `low`, `medium`, or `high` |
| `scope` | `task`, `artifact`, `session`, or `system` |
| `evidence_refs` | References to the observations that justify the signal |
| `required_action` | Machine-routable action: `surface`, `reclassify`, `recover`, `confirm`, `refuse`, `constrain`, `reinspect`, `suppress` |
| `detail_type` | Optional refinement for a canonical family signal (e.g. `n_plus_one_query`) |
| `subject_ref` | Optional reference to the artifact/claim/entity the signal is about |

The envelope carries no SISPIS scoring. SISPIS owns every mapping from a
semantic signal to entropy/intent/output-floor; upstream protocols emit
meaning, not calibration math. SISPIS deduplicates by `cause_id` before
calibrating.
