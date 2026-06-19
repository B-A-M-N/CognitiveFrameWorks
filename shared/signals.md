# Shared Signal Registry

Cross-skill signal primitives. These signals are shared vocabulary understood by multiple skills. Each skill may also define its own local signal details in its reference files.

## Signal Types

| Signal | Description | Emitted by | Consumed by |
|--------|-------------|------------|-------------|
| `verification_needed` | A claim requires verification before it can be treated as established | OWL | FUSE, SISPIS |
| `approach_failed` | Current reasoning or implementation approach is not working | OWL | ANCHOR, SISPIS |
| `constraint_drift` | The constraints of the task have shifted from what was originally established | OWL, ANCHOR | SISPIS |
| `retry_bound_exceeded` | Tool execution has exceeded retry bounds without success | FUSE | ANCHOR, SISPIS |
| `authority_risk` | An action carries authority, permission, or scope risk | WARD | ANCHOR, SISPIS |
| `secret_exposure` | An action would expose or transmit secrets | WARD | ANCHOR, SISPIS |
| `evidence_overclaim` | A tool result is being interpreted more broadly than it warrants | FUSE | ANCHOR, SISPIS |
| `operational_drag` | The produced code creates measurable operational friction or load | FLOW | SISPIS, ANCHOR |
| `recovery_started` | ANCHOR has initiated a recovery procedure | ANCHOR | OWL, FUSE, SISPIS |
| `confirmation_required` | An action requires user confirmation before proceeding | WARD | SISPIS |

## Scope

This registry contains only cross-skill primitives — signals that are transmitted between skills. Skill-specific internal signals (e.g., FLOW's `n_plus_one_query`, WARD's `untrusted_execution`) are maintained in each skill's own reference files and are not listed here.

## SISPIS Delta Mapping

When a shared signal adjusts SISPIS entropy dimensions, the owning skill's SKILL.md or references define the specific delta. This registry does not duplicate those mappings — it exists to prevent naming collisions and ensure consistent vocabulary across skills.
