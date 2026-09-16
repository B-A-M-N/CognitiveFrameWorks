# CognitiveFrameworks

Behavioral protocols for AI assistants. Seven skills that operate as a pipeline, and can also be used independently.

## Skills

- **OWL** — Operational Wisdom Layer. Pre-implementation reasoning protocol. Runs a nine-principle pass before acting; treats user-reported behavior as evidence requiring reinspection, rejects circular verification, and detects responsibility-concentration risk before edits.
- **ANCHOR** — Agent Nexus for Context History and Operational Recovery. Preserves execution continuity by maintaining state integrity, object continuity, memory integrity, epistemic classification, reopen lifecycle, recovery discipline, completion criteria, action accountability, and information economy throughout a session.
- **DOX** — Documentation Operations eXchange. AGENTS.md hierarchy protocol. Loads the applicable doc contract before editing, runs a closeout pass after. Preserves repo-local documentation as a binding work contract.
- **SISPIS** — Skank in Sheets, Princess in Streets. Decision-routing and response calibration. Gates output structure based on entropy. Simple queries get direct answers; decision-laden queries get a structured framework.
- **FUSE** — Functional Utility Selection & Execution. Governs tool-call strategy: when fresh evidence is required, which tool, in what order, when to parallelize, what bounds, what results prove, when stale or circular evidence cannot certify success, when to stop retrying, and when to avoid tools. Wraps the execution layer — runs per-action, not per-request.
- **FLOW** — Friction, Load, Overhead, and Workload. Evaluates the implementation for operational drag across eight principles: Retry Discipline, Backpressure, Cache Hygiene, Startup Efficiency, Hot-Path Awareness, External I/O Discipline, Workflow Friction, and Maintenance Weight. Maintenance Weight covers excessive abstraction and responsibility concentration / God Objects. Triggered, not always-on — the trigger gate is the primary suppression. Runs after FUSE-governed execution, before DOX closeout.
- **WARD** — Warning Authority Risk Director. Gates whether a selected action is allowed, safe enough, too destructive, exposes secrets, crosses a trust boundary, or requires confirmation. Eight principles — Authority Fit, Blast Radius, Secret Hygiene, Trust Boundary Discipline, Mutation Consent, Reversibility, Dependency & Supply-Chain Caution, and Policy Preservation. Runs alongside FUSE at each action decision point. WARD can veto, constrain, or require confirmation for a FUSE-selected action.

## Pipeline Order

When used together, the canonical order is: OWL preflight → ANCHOR state baseline → DOX load (if editing a DOX-governed project) → FUSE + WARD wrap every action/tool decision → artifact produced → FLOW evaluates the artifact if a trigger fires → DOX closeout (if docs/contracts changed) → ANCHOR checkpoint/reclassification if needed → SISPIS final output. Canonical integration details live in `shared/integration.md`. OWL signals feed into SISPIS via pre-gate hook. ANCHOR maintains operational state between reasoning and action. DOX-load happens before the edit; DOX-closeout happens after FLOW. FUSE + WARD are not a linear stage — they govern tool selection, evidence interpretation, and authority/blast-radius gating at each action point. FLOW runs once per artifact, only if a trigger fires, before DOX closeout.

```
Request → OWL → ANCHOR → DOX (load) → FUSE + WARD → Edit → FLOW → DOX (closeout) → SISPIS → Output
```

FUSE + WARD wrap the execution layer — they run at each tool-call decision point, not as a single linear stage.

FLOW runs once per artifact, after FUSE-governed execution — it evaluates the produced code for operational drag, not at each action point.

## DOX Closeout Policy
DOX closeout updates documentation only when the change alters durable contracts, ownership, scope, workflow, permissions, recurring operating rules, or child index structure. One-off FLOW tradeoffs stay in ANCHOR unless they establish a durable project constraint, recurring exception, or future implementation rule.

DOX does not create a `FLOW_ISSUES.md` unless the user explicitly asks for durable issue tracking.

## Canonical Contracts

Cross-skill semantics are owned by machine-checkable files, not prose:

- `shared/signals.md` — canonical namespaced signal registry
- `shared/signal.schema.json` — canonical signal envelope (JSON Schema)
- `shared/signal-registry.json` — machine-readable registry
- `shared/pipeline.yaml` — canonical pipeline manifest (stage, activation rule, owner, input/output signal types, required predecessors, suppression, runtime cost class)
- `shared/integration.md` — the cross-skill integration contract

Upstream protocols (OWL, ANCHOR, FUSE, FLOW, WARD) emit **semantic signals
only** in the canonical envelope. They do not compute SISPIS entropy, intent
weight, or output floors; SISPIS alone owns signal → calibration mapping.
Run `scripts/validate-framework.py` and `scripts/doctor.py` after changing any
contract file or skill.

## Core Evidence Doctrine

Two principles imported from the CognitiveStateWork skills (gitter/getter) apply far beyond Git and bind every protocol in this framework:

1. **Agent action is not evidence of correctness.** An agent saying "implemented," "fixed," or "done" cannot close an observation by itself; the result must be independently re-observed or validated. A command's exit code, a passing test that doesn't target the claim, or a prior conclusion in the session are all construction facts, not proof.

2. **User/operator observations invalidate stale assumptions.** When the operator reports "no, that is still happening" or "you're looking at this wrong," treat it as a state-invalidating observation: reopen the relevant state and re-inspect. Do not defend the earlier model or explain why the earlier work ought to have worked.

These are the framework-level contracts; gitter and getter (in `~/CognitiveStateWork/`) are the Git-specific procedural instantiations of them and remain independent plug-in skills, not part of the core pipeline.

## Pipeline Budget

Running the full seven-skill pipeline on every request is unnecessary overhead. Apply the minimum subset that serves the task:

| Task type | Load |
|-----------|------|
| Simple factual / explanatory answer | SISPIS only, or OWL + SISPIS if reasoning risk exists |
| Single-turn code advice | OWL + SISPIS |
| Multi-turn debugging | OWL + ANCHOR + FUSE + SISPIS |
| Tool-using implementation | OWL + ANCHOR + FUSE + WARD + SISPIS |
| Performance / operational drag review | OWL + FLOW + SISPIS |
| File-editing in DOX-governed project | OWL + ANCHOR + DOX + FUSE + WARD + FLOW + SISPIS as needed |
| Risky commands / secrets / external effects | WARD required |

These ownership contracts explicitly target **self-certification / dismissal of contradictory user observations** and **responsibility concentration / God Objects**. A user report of observed behavior is evidence requiring reinspection; it does not automatically prove the user's root cause. Simplicity is not file count when responsibility ownership deteriorates.

**Loading a skill is itself overhead.** The minimum sufficient subset is required by ANCHOR's Information Economy and FLOW's Maintenance Weight principles.

**Rules:**
- DOX only activates when editing files in a DOX-enabled project (one with AGENTS.md). It does not run on conversational or analysis-only tasks.
- ANCHOR only activates when a task spans multiple turns or involves state that would be costly to lose. It does not run on tasks under 5 turns with no state accumulation.
- OWL always runs unless the task is purely mechanical with no code to read and no ambiguity (see OWL Suppression Conditions in `references/signal-schema.md`).
- FUSE activates whenever tools are being called. It suppresses for single obvious calls (known target, only fitting tool, no retry/parallel/evidence ambiguity). It does not run on conversational or analysis-only tasks.
- WARD activates whenever tools can mutate state, access private data, call network resources, run commands, install dependencies, send messages, or expose secrets. It suppresses for pure reads of project files with no boundary crossing, no secret exposure, and no policy implication.
- FLOW activates only when the implementation touches one of eleven trigger areas:
  1. Retry/backoff logic
  2. Queues/streams/buffers
  3. Caches (invalidation, eviction, consistency)
  4. Startup/shutdown sequences
  5. Hot paths (frequently executed code)
  6. Build/test/CI pipelines
  7. Provider/API calls (network, external services)
  8. Database/filesystem operations
  9. Complex abstractions (e.g., metaprogramming, reflection)
  10. Responsibility concentration / central-object growth
  11. Maintenance burden (e.g., deprecated APIs, technical debt)
  If no trigger fires, FLOW does not run. It does not optimize for cleverness or micro-performance — only measurable or likely operational drag.
- SISPIS always runs — it is the output gate and has negligible cost when it resolves to NO_DECISION.

## SISPIS Signal Integration Protocol

Upstream skills (OWL, FUSE, WARD, FLOW, ANCHOR) emit semantic signals in the
canonical envelope (`shared/signal.schema.json`). They never compute SISPIS
scoring. SISPIS alone owns every signal → calibration mapping:

```text
canonical envelope
  → deduplicate by cause_id (one cause adjusts response structure once)
  → look up signal_type in SISPIS/references/signal-calibration.yaml
  → apply SISPIS-owned calibration (entropy deltas, intent weight)
  → apply required_action floor (minimum_mode)
  → run decision gate
```

The calibration table is the only scoring table. DOX is not an upstream
signal source; it surfaces contract constraints that OWL, ANCHOR, and FUSE
may consume.

### Conflict Resolution
If WARD refuses a FUSE-selected action, the following occurs:
- FUSE logs the refusal and marks the action as blocked.
- SISPIS receives the WARD refusal signal; the `refuse` / `recover`
  required-action floor forces output to at least EXPLANATION via its own
  calibration.
- ANCHOR records the veto in the session state for recovery or audit purposes.
- The pipeline halts at the FUSE+WARD stage; no further actions (e.g., edits,
  tool calls) proceed.

### Signal Deduplication
Multiple skills may emit on the same underlying cause (e.g., OWL
`approach_failed`, FUSE `retry_bound_exceeded`, ANCHOR `recovery_started`
describing one failed retry loop). All envelopes sharing a `cause_id` are
collapsed into a single calibration application before the gate runs.
Independent causes compound.

## Skill Availability

Seven skills are defined in this repo: OWL, ANCHOR, DOX, FUSE, FLOW, WARD, and SISPIS.

Load the minimum subset that serves the task. Do not load the full set by default; full-skill loading is only appropriate when the task explicitly requires it. If a platform exposes a `skill` loader, use it only for the active subset. Runtime availability is separate from repo availability.

Canonical integration details and adapter generation live in `shared/integration.md` and `shared/adapter-source.md`. SISPIS's signal → calibration scoring table is `SISPIS/references/signal-calibration.yaml`.
