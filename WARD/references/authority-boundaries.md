# WARD Authority Boundaries — Integration Spec

## Signal Type Registry

Complete registry of all signal types, organized by principle. Each signal carries a weight and a default required action. The required action is WARD's decision on the action; the weight determines stacking priority and SISPIS entropy elevation.

### Authority Fit

| Signal Type | Weight | Required Action | Condition |
|-------------|--------|-----------------|-----------|
| `authority_exceeded` | 2.0 | refuse | The action exceeds the authority the user granted — "fix the bug" does not authorize deploying, "clean temp" does not authorize deleting data |
| `authority_unknown` | 1.0 | confirm | Authority is ambiguous for a consequential action and cannot be resolved from context or DOX contracts |
| `state_recovery_required` | 1.0 | recover | Prior state or authority baseline is stale/corrupted, so WARD cannot safely evaluate a new action until ANCHOR recovers state |

### Blast Radius

| Signal Type | Weight | Required Action | Condition |
|-------------|--------|-----------------|-----------|
| `destructive_irreversible` | 2.0 | refuse | The action is destructive and cannot be undone — `rm -rf` on data, `DROP TABLE`, format, force-overwrite without backup. Refused unless Reversibility establishes a recovery path |
| `broad_blast_radius` | 0.5 | constrain | The action affects scope beyond its immediate target — recursive, global, system-wide. Constrain to the narrowest scope that accomplishes the task |

### Secret Hygiene

| Signal Type | Weight | Required Action | Condition |
|-------------|--------|-----------------|-----------|
| `secret_exposure` | 2.0 | refuse | An action would expose a secret in output, logs, or an unscoped transmission — embedding a token in a shell command, logging an env var, transmitting credentials to an untrusted endpoint |
| `secret_in_context` | 1.0 | constrain | A secret is being read into context where it may be surfaced — reading `.env`, credentials files, or secret directories when not explicitly required by the task. Constrain: redact, skip, or read only the needed field |

### Trust Boundary Discipline

| Signal Type | Weight | Required Action | Condition |
|-------------|--------|-----------------|-----------|
| `boundary_crossing` | 1.0 | constrain | The action crosses a trust boundary — project → user/private/system, local → external. Constrain: name the boundary, verify the crossing is justified |
| `unscoped_external_call` | 1.0 | confirm | An external network call to a destination not implied by the task — posting data, calling an unverified API, sending to an external service. Confirm before executing |

### Mutation Consent

| Signal Type | Weight | Required Action | Condition |
|-------------|--------|-----------------|-----------|
| `unconfirmed_mutation` | 1.0 | confirm | A destructive, external, or out-of-scope mutation is about to occur without confirmation |
| `external_side_effect` | 1.0 | confirm | The action produces an external side effect — send, publish, deploy, post to an API. External side effects require confirmation because they cannot be undone from within the project |

### Reversibility

| Signal Type | Weight | Required Action | Condition |
|-------------|--------|-----------------|-----------|
| `missing_reversibility` | 1.0 | constrain | A risky mutation is about to occur without a backup, diff, or checkpoint. Constrain: create a reversal path before mutating |
| `irreversible_write` | 1.0 | confirm | An overwrite would destroy prior state with no recovery path and no reversal path can be created. Confirm with the user |

### Dependency & Supply-Chain Caution

| Signal Type | Weight | Required Action | Condition |
|-------------|--------|-----------------|-----------|
| `supply_chain_risk` | 1.0 | confirm | A dependency is being installed from an unverified or unfamiliar source, or a package includes install-time code execution (postinstall hooks, network calls) |
| `untrusted_execution` | 2.0 | refuse | Code from an untrusted source is about to be executed — downloaded script, unverified package, code from an untrusted endpoint |

### Policy Preservation

| Signal Type | Weight | Required Action | Condition |
|-------------|--------|-----------------|-----------|
| `policy_conflict` | 1.0 | refuse | The action conflicts with a DOX-loaded contract constraint — forbidden action, deployment rule, secrets handling policy, destructive-operation constraint |
| `permission_widening` | 1.0 | constrain | The action requires widening permissions beyond what policy allows to make progress. Constrain: surface the conflict; do not widen silently |

---

## Weight Summary

| Weight | Signal Types |
|--------|-------------|
| 2.0 | `authority_exceeded`, `destructive_irreversible`, `secret_exposure`, `untrusted_execution` |
| 1.0 | `authority_unknown`, `boundary_crossing`, `unscoped_external_call`, `unconfirmed_mutation`, `external_side_effect`, `missing_reversibility`, `irreversible_write`, `supply_chain_risk`, `policy_conflict`, `permission_widening`, `state_recovery_required` |
| 0.5 | `broad_blast_radius` |

2.0 signals are refuse-level: they block the action unless the finding is resolved (reversibility established, secret redacted, authority confirmed). These are the highest-impact WARD findings — they represent actions that would cause irreversible harm if executed.

1.0 signals are constrain/confirm-level: they modify the action or require user consent before proceeding.

0.5 signals are caution-level: they surface informational risk without blocking.

---

## Required Action Taxonomy

The `required_action` field is WARD's decision on the action. It is the decisive output — more important than the weight for the proceed/block decision.

| Required Action | Meaning | FUSE Response | ANCHOR Response | SISPIS Response |
|----------------|---------|---------------|-----------------|-----------------|
| `proceed` | Action is within authority, safe enough, and reversible or justified | Execute normally | Record action | No forced elevation |
| `constrain` | Action is permitted but must be modified — scoped, backed up, redacted | Re-plan with WARD's constraints, then execute | Record original + constrained action | May elevate if risk is user-visible |
| `confirm` | Action requires user consent before execution | Halt. Do not execute until confirmed | Record the pending confirmation | Force at least EXPLANATION — user must see risk to consent |
| `refuse` | Action is not permitted — exceeds authority, destroys irreversibly, exposes secrets, or violates policy | Do not execute. Escalate to ANCHOR Recovery | Recovery Discipline trigger — approach must change | Force at least EXPLANATION — user must see why blocked |
| `recover` | State recovery is required before the action can proceed | Halt. Do not execute | Recovery Discipline — recover state first, then re-evaluate | May elevate if recovery is user-visible |

---

## Authority Classification

WARD classifies every consequential action's authority into one of four states:

| Authority State | Meaning | Default Required Action |
|----------------|---------|------------------------|
| `granted` | The user explicitly requested this action or it is clearly within the scope of the request | proceed |
| `requested` | The action is within the spirit of the request but not explicitly requested — inferred authority | proceed for low-risk; constrain for medium-risk; confirm for high-risk |
| `exceeded` | The action goes beyond what the user asked for — broader scope, different target, different action type | refuse |
| `unknown` | Authority cannot be determined from the request or DOX contracts, and the action is consequential | confirm |

### Authority Assessment Procedure

1. Parse the user's request for explicit action verbs and scope: "fix the bug in auth.py" → edit auth.py (granted), run tests (granted), deploy (exceeded).
2. Check DOX contracts for authority constraints: does the loaded AGENTS.md define forbidden actions, deployment rules, or permission boundaries?
3. Classify the proposed action's authority: does it fall within granted, requested, exceeded, or unknown?
4. If `granted` and blast radius is low → proceed (silent).
5. If `requested` → assess blast radius to determine constrain vs. confirm.
6. If `exceeded` → refuse (surface the scope expansion).
7. If `unknown` and consequential → confirm.

---

## Trust Boundary Taxonomy

WARD distinguishes five boundary levels. Crossing from a lower-numbered to a higher-numbered boundary is a boundary crossing that must be acknowledged.

| Level | Boundary | Examples |
|-------|----------|----------|
| 1 | Local/project | Files within the project directory, project config, project dependencies |
| 2 | User | User home directory, user config files (`~/.gitconfig`, `~/.ssh`), user shell config |
| 3 | Private/system | System directories (`/etc`, `/var`), other users' files, system services |
| 4 | External/network | Network calls to APIs, external services, web endpoints |
| 5 | Publish/broadcast | Actions that send data outward — publish, post, deploy, send email, push to remote |

Crossing from level 1 → 2+ is a `boundary_crossing` signal (constrain). Actions at level 4 that are not implied by the task are `unscoped_external_call` (confirm). Actions at level 5 are `external_side_effect` (confirm).

---

## Secret Detection Heuristics

WARD checks for secrets before actions that read, write, log, or transmit content. Secret indicators:

- Environment variable names containing: `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASS`, `CREDENTIAL`, `PRIVATE`, `API`
- Files named: `.env`, `.env.*`, `*credentials*`, `*secret*`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`
- Content patterns: `AKIA...` (AWS keys), `ghp_...` (GitHub tokens), `sk-...` (Stripe/API keys), `-----BEGIN ... PRIVATE KEY-----`, `password=`, `token=`, `api_key=`
- Connection strings containing embedded credentials: `protocol://user:password@host`

When a secret is detected:

- If the action would **log or transmit** it → `secret_exposure` (refuse)
- If the action would **read it into context** and it's not needed → `secret_in_context` (constrain: skip the read)
- If the action **requires** the secret (the task explicitly needs the credential) → proceed, but redact in any output

---

## Gate Threshold

```
W_ward = sum of all emitted signal weights

Hard gate (checked first, per signal):
  Any signal with required_action = refuse    → Block. Surface with refuse.
  Any signal with required_action = confirm   → Halt. Surface and request confirmation.
  Any signal with required_action = recover   → Halt. Surface and require recovery.
  Any signal with required_action = constrain → Surface. Action modified before execution.

Soft gate (checked if no hard gate fired):
  W_ward >= 1.5  →  Surface mode (informational risk surfacing)
  W_ward <  1.5  →  Silent mode

Suppression condition:
  Pure read of project files, no boundary crossing, no secret exposure,
  no policy implication → WARD does not run. W_ward = 0.
```

The hard gate takes absolute precedence. A single `refuse` signal blocks the action regardless of the total weight. The soft gate surfaces informational risk that doesn't require blocking but that the user should know about.

---

## Suppression Condition

WARD does not run when all of the following are true:

- The action is a read (no mutation)
- The target is within the project directory (no boundary crossing)
- The target does not match secret indicators (no secret exposure)
- No DOX contract constrains reads of this target (no policy implication)
- The action does not access network resources (no external call)

This is the primary suppression mechanism — not a weight threshold, but a domain gate. If the action is a pure in-scope read with no secrecy or policy implications, WARD is silent by default.

---

## Integration Points

### FUSE → WARD

FUSE calls WARD after selecting an action but before executing it, when the action falls into any of these categories:

| Action Category | WARD Principles That Fire |
|----------------|---------------------------|
| File write/edit/delete | Blast Radius, Mutation Consent, Reversibility |
| Command execution | Authority Fit, Blast Radius, Secret Hygiene, Trust Boundary |
| Network call | Trust Boundary, Secret Hygiene, Mutation Consent (if POST/PUT/DELETE) |
| Dependency install | Supply-Chain Caution, Authority Fit, Policy Preservation |
| File read outside project | Trust Boundary, Secret Hygiene |
| File read of secret indicators | Secret Hygiene |
| Deploy/publish/send | Mutation Consent, Trust Boundary, Authority Fit |
| Permission change | Policy Preservation, Authority Fit |

FUSE does not call WARD for: pure reads of project files that don't match secret indicators, `ls`/`glob`/`grep` within the project, `view` of project source files.

### WARD → ANCHOR

| WARD event | ANCHOR response |
|------------|-----------------|
| Action refused (`refuse`) | Recovery Discipline trigger — approach must change. The selected action is not permitted; a different approach is needed. |
| Risk accepted by user (`confirm` → user approved) | Action Accountability records: action, risk, user decision, rationale |
| Authority exceeded | State Integrity classifies the authority boundary as a constraint for future actions |
| Constrained action | Action Accountability records: original action, WARD constraint, modified action |
| State recovery required (`recover`) | Recovery Discipline — recover state, then re-evaluate the action |

### DOX → WARD

DOX Phase 1 surfaces authority-relevant constraints that WARD consumes as binding policy:

| DOX contract content | WARD principle that consumes it |
|---------------------|-------------------------------|
| Forbidden actions list | Policy Preservation (treat as `policy_conflict` if action matches) |
| Deployment rules | Authority Fit (deploy is `authority_exceeded` unless rules are followed) |
| Secrets handling policy | Secret Hygiene (policy defines what constitutes a secret and how to handle it) |
| Destructive-operation constraints | Blast Radius / Reversibility (policy defines required backups or forbidden destructive ops) |
| Permission boundaries | Policy Preservation / Authority Fit (policy defines what authority the agent has) |

DOX does not evaluate security posture. It surfaces the contract; WARD evaluates the action against it.

### WARD → SISPIS

WARD elevates SISPIS output mode based on required action:

| WARD required action | SISPIS minimum output mode | Rationale |
|---------------------|---------------------------|-----------|
| `proceed` | (no forced elevation) | Action is safe; SISPIS operates normally |
| `constrain` | (no forced elevation, but entropy elevated) | Action was modified; risk is informational |
| `confirm` | EXPLANATION minimum | User must see the risk to consent |
| `refuse` | EXPLANATION minimum | User must see why the action was blocked |
| `recover` | EXPLANATION minimum | User must see that recovery is occurring |

SISPIS entropy deltas from WARD signals (apply before Stage 1, cap at 2.0):

| WARD signal | SISPIS signal | Delta |
|-------------|---------------|-------|
| `authority_exceeded` | `tradeoff_density`, `downstream_impact` | +2 each |
| `destructive_irreversible` | `downstream_impact` | +2 |
| `secret_exposure` | `downstream_impact` | +2 |
| `untrusted_execution` | `downstream_impact` | +2 |
| `boundary_crossing` | `tradeoff_density` | +1 |
| `unscoped_external_call` | `tradeoff_density`, `downstream_impact` | +1 each |
| `unconfirmed_mutation` | `tradeoff_density` | +1 |
| `external_side_effect` | `downstream_impact` | +1 |
| `supply_chain_risk` | `tradeoff_density` | +1 |
| `policy_conflict` | `tradeoff_density` | +1 |
| `permission_widening` | `tradeoff_density` | +1 |
| `missing_reversibility` | `downstream_impact` | +1 |
| `irreversible_write` | `downstream_impact` | +1 |
| `secret_in_context` | `tradeoff_density` | +1 |
| `broad_blast_radius` | `downstream_impact` | +0.5 |

### WARD → FLOW

When WARD accepts a risk-laden tradeoff (user confirms a risky action, or WARD constrains but allows an action with residual risk), the accepted tradeoff is fed to FLOW:

| Accepted WARD tradeoff | FLOW principle that evaluates |
|----------------------|------------------------------|
| Aggressive retry against external API (user confirmed) | Retry Discipline — will the retry loop cascade under load? |
| Unbounded queue in a security-sensitive path (user confirmed) | Backpressure — will the unbounded queue leak memory at scale? |
| Elevated permissions for a privileged operation | Maintenance Weight — will the permission elevation create ongoing operational burden? |
| Accepted supply-chain risk (unverified dependency installed) | Maintenance Weight — will the unverified dependency require ongoing scrutiny? |

WARD owns whether the tradeoff is allowed. FLOW owns whether the accepted tradeoff creates ongoing operational drag. Both can fire on the same action for different reasons.
