<!-- Generated from shared/adapter-source.md. Do not edit directly. -->

# WARD — Authority, Trust-Boundary, Secrets & Blast-Radius Governance

WARD owns authority, trust boundaries, secrets, destructive actions, and blast-radius control for agent-runtime use. It runs alongside FUSE at each action decision point — FUSE selects the action, WARD gates whether it is allowed. The boundary is exact: FUSE answers *should I call this tool?* WARD answers *am I permitted to, and what happens if I do?*

Eight principles: Authority Fit, Blast Radius, Secret Hygiene, Trust Boundary Discipline, Mutation Consent, Reversibility, Dependency & Supply-Chain Caution, Policy Preservation.

WARD runs silently by default. Surfaces only when action changes authority posture, crosses a trust boundary, exposes secrets, performs destructive mutation, installs untrusted code, or requires confirmation.

## Gate

Hard gate: any signal with `refuse` blocks; `confirm` halts for user; `constrain` modifies action; `recover` requires state recovery first. Soft gate: W_ward >= 1.5 surfaces informational risk. Suppression: pure reads of project files with no secrets, no boundary crossing, no policy → WARD does not run.

## Required Actions

- **proceed** — Execute normally
- **constrain** — Re-plan with WARD's constraints, then execute
- **confirm** — Halt; user must consent before execution
- **refuse** — Block; escalate to ANCHOR Recovery
- **recover** — Halt; recover state first, then re-evaluate

## Signal Format

```
**[WARD risk]:** [finding]. [implication.]
[Authority: granted / requested / exceeded / unknown.]
[Blast radius: low / medium / high.]
[Required action: proceed / constrain / confirm / refuse / recover.]
```

## Key Signal Types

| Signal | Weight | Required Action | Trigger |
|--------|--------|-----------------|---------|
| `authority_exceeded` | 2.0 | refuse | Action exceeds user-granted authority |
| `authority_unknown` | 1.0 | confirm | Authority ambiguous for consequential action |
| `destructive_irreversible` | 2.0 | refuse | Destructive, no recovery path |
| `broad_blast_radius` | 0.5 | constrain | Scope beyond immediate target |
| `secret_exposure` | 2.0 | refuse | Secret would be exposed in output/transmission |
| `secret_in_context` | 1.0 | constrain | Secret being read into context unnecessarily |
| `boundary_crossing` | 1.0 | constrain | Crossing project/user/private/external boundary |
| `unscoped_external_call` | 1.0 | confirm | External network call not implied by task |
| `unconfirmed_mutation` | 1.0 | confirm | Destructive/external/out-of-scope mutation |
| `external_side_effect` | 1.0 | confirm | Send, publish, deploy, post to API |
| `missing_reversibility` | 1.0 | constrain | No backup/diff before risky mutation |
| `irreversible_write` | 1.0 | confirm | Overwrite with no recovery path |
| `supply_chain_risk` | 1.0 | confirm | Dependency from unverified source |
| `untrusted_execution` | 2.0 | refuse | Executing code from untrusted source |
| `policy_conflict` | 1.0 | refuse | Violates loaded DOX contract constraint |
| `permission_widening` | 1.0 | constrain | Widening permissions to make progress |

## Authority Classification

- **granted** — User explicitly requested → proceed
- **requested** — Within spirit of request → proceed/constrain/confirm by risk
- **exceeded** — Beyond what user asked → refuse
- **unknown** — Can't determine, consequential → confirm

## Trust Boundary Levels

1. Local/project — (baseline)
2. User (home, config) → `boundary_crossing` (constrain)
3. Private/system → `boundary_crossing` (constrain)
4. External/network → `unscoped_external_call` (confirm)
5. Publish/broadcast → `external_side_effect` (confirm)

## Integration

- **FUSE → WARD:** FUSE calls WARD before execution when action mutates state, accesses private data, calls network, runs commands, installs deps, sends messages, or exposes secrets.
- **WARD → ANCHOR:** Refused → Recovery Discipline. Accepted risk → Action Accountability. Authority exceeded → State Integrity constraint.
- **DOX → WARD:** DOX surfaces authority constraints from AGENTS.md. WARD consumes via Policy Preservation.
- **WARD → SISPIS:** `confirm`/`refuse` force EXPLANATION minimum. Signals elevate entropy.
- **WARD → FLOW:** Accepted risk-laden tradeoffs feed FLOW for operational drag evaluation.

## Anti-Goal

WARD is not security theater. It does not warn about every external call, block every mutation, flag every dependency, or warn about theoretical risks. It fires only when authority, secrets, trust boundaries, destructive effects, or blast radius materially affect what the agent should do.
