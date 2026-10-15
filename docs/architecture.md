# Architecture

Task policy, authority, lifecycle, and composition for CognitiveFrameWorks.

## What CognitiveFrameWorks governs

CFW owns reusable cognitive components and their enforcement contracts. DP owns
the cross-component behavioral state and adaptation policy. In combined mode, DP
supplies global semantic interpretation and behavior selection while CFW keeps
its local gates. CFW rejects provider injection and no longer carries
semantic-provider policy in task bundles; configure semantic assessment in DP.
Standalone CFW uses its deterministic fallback over host facts. Action accounting
follows the host descriptor or objective binding; semantic output cannot relabel
it.

It governs things such as:

* how the agent distinguishes observation from inference;
* when it should inspect before acting;
* how it handles contradictory evidence;
* how failed approaches are abandoned rather than endlessly patched;
* what tool use is justified;
* what a tool result actually proves;
* which actions are authorized;
* when an action requires confirmation;
* how blast radius and reversibility affect execution;
* when operational complexity deserves attention;
* how completion claims are validated;
* how conclusions are communicated at the appropriate evidentiary altitude.

CFW does **not** try to encode every domain workflow. That belongs to
CognitiveStateWorks. CFW also does not decide that a behavioral tendency exists
merely because an agent or operator says so. That belongs to DigitalPsychology.

## The seven components

| Component | Responsibility                                                     |
| --------- | ------------------------------------------------------------------ |
| **OWL**   | Reasoning discipline, epistemics, locality, conservation, verification |
| **ANCHOR** | Operational continuity, recovery, state integrity, completion discipline |
| **DOX**   | Documentation and repository-contract hierarchy                    |
| **FUSE**  | Tool selection, sequencing, evidence interpretation, retry/termination discipline |
| **WARD**  | Authority, trust boundaries, destructive actions, secrets, reversibility |
| **FLOW**  | Operational drag, scalability, retries, backpressure, hot paths, maintenance weight |
| **SISPIS** | Communication and output calibration                               |

These are not intended to become seven giant prompt blocks loaded on every
request. CFW uses **progressive disclosure and task composition**: activate the
smallest legal behavioral surface that serves the task.

The component catalog assigns each component a `model_visibility` policy:
`guidance` may enter context, while `consequence_only` and `none` stay outside
the prompt. DP-controlled prompts omit pipeline labels and internal contract
hashes; host-side gates still enforce the underlying behavior.

## Current implementation

CognitiveFrameWorks is implemented as task-local enforcement and a component
policy compiler:

- **Standalone behavior authority** gives CFW deterministic behavior and route
  policy when DP is absent. The trusted host still owns the
  `TaskGoalContract`, its acceptance criteria, and the completion evidence.
- **Task authority** pins the goal, structural eligibility, component floors, and
  local enforcement policy. Revisioned behavioral overlays may adjust optional
  behavior without changing those authority boundaries.
- **Capability composition** supports CFW-only, CFW + CSW, CFW + DP, and the full
  CFW + CSW + DP deployment. A missing StateWork checkout does not prevent an
  ordinary CFW-only task from resolving.
- **Safe adaptive advice** is applied only after the host computes eligible
  choices. CFW exports a task-specific behavior contract and rejects any
  DigitalPsychology projection below a selected component's baseline floor.
- **Action authority** remains host-owned. `ActionStrategy` provides bounded
  preference hints, but every action still passes the structural action gate.
- **Evidence and telemetry** are separated: the runtime keeps local NDJSON
  telemetry, while the optional exporter provides bounded, cursor-based,
  event-ID-idempotent delivery to an external service.
- **StateWork integration** accepts only workflows eligible under CSW's current
  authoritative selection and delegates legal state transitions and evidence
  checks to CSW.

## Runtime model

At runtime, a task is resolved into a pinned structural authority contract.
Optional behavior can change through revisioned host-applied overlays, subject to
CFW's component floors and action gates.

```text
TaskRequest
    │
    ├── immutable goal contract when completion is expected
    │     ├── primary outcome / deliverable / work mode
    │     ├── required acceptance criteria and non-goals
    │     └── proportional verification policy
    ├── task shape
    ├── domain
    ├── operation / phase
    ├── model / harness / toolset
    ├── authority context
    └── agent identity
            │
            ▼
    Static eligibility
            │
            ├── framework stages
            ├── StateWorks
            ├── flows
            ├── specialists
            └── guards
            │
            ▼
   Host/CSW workflow plan
   (when a workflow provider is enabled)
            │
            ▼
     Pinned CFW task authority
             ▲
             │ revisioned optional overlay
        DigitalPsychology
            │
            ▼
       RuntimeSession
            │
            ├── model calls
            ├── action gates
            ├── evidence ledger
            ├── StateWork transitions
            ├── completion boundary
            └── behavioral telemetry
```

The goal contract is hashed into `EffectivePolicy`, the pinned task snapshot,
routing context, and task manifest. A compact form leads every model context, and
completion claims bind to its hash and the criterion IDs they prove. Hosts should
pass the user's objective and acceptance criteria in
`TaskRequest.goal_contract`. Legacy requests can still run, but successful
completion is blocked until the host pins a goal contract with at least one
required criterion and supplies host-attested evidence for each required claim.

The CFW criterion ledger supports the full bounded task set (up to 256 criteria).
In a three-system runtime, DP may grant a short attention lease for up to three
criteria; CFW projects that active window while the host retains and verifies the
complete goal contract. Criteria outside the current window remain in CFW's ledger
and the host's completion check.

Lifecycle stages use their declared frequencies: per-action wrappers expire when
the host completes the action, per-artifact checks expire after host postflight
processing, and response calibration expires when output rendering finishes. Model
tool capability alone does not activate action-planning stages; the host calls the
explicit planning boundary when an action is being chosen.

## Fast loop and slow loop

The architecture deliberately separates two timescales.

### Fast loop — current work

The trusted host coordinates current-task behavior. CFW enforces component and
action authority, CSW owns workflow legality and evidence, and DP may adjust
optional behavior through bounded host-applied decisions:

```text
host event
→ CFW and CSW provide bounded state
→ DP selects an optional behavioral adjustment
→ host applies legal CFW/CSW changes
→ agent acts
→ host records evidence and outcomes
```

Structural authority remains pinned for the task. Optional behavioral overlays
are revisioned and recorded so the host can reproduce the active projection.

### Slow loop — learning across work

DigitalPsychology observes many real sessions:

```text
telemetry
→ episodes
→ task trajectories
→ session trajectories
→ behavioral patterns
→ controlled experiments
→ validated intervention
→ future policy
```

That allows the system to improve future behavior **without continuously
rewriting the rules underneath a task that is already running**.

## Session-aware telemetry

CFW emits structured behavioral telemetry without requiring DigitalPsychology
itself to be loaded into the agent.

Runtime identity distinguishes:

```text
agent instance
    └── session
         └── task
              └── attempt
                   └── execution segment
                        └── event
```

Delegated and multi-agent work may additionally carry interaction,
parent-session, delegator, delegate, role, and delegation identities. This
prevents unrelated sessions from being accidentally collapsed into one behavioral
history. The runtime's security-sensitive authority identities remain separate
from analytics identities.

## The larger architecture

CognitiveFrameWorks is one of three complementary systems:

| System | Primary question | Role |
| --- | --- | --- |
| **CognitiveFrameWorks** | Which cognitive mechanisms and guards are available? | Reusable reasoning components, execution discipline, authority, evidence, recovery, communication |
| **CognitiveStateWorks** | Which workflow states and transitions are legal now? | Domain state, workflow legality, transition requirements, handoffs, recovery |
| **DigitalPsychology** | How should behavior adapt, and did the intervention improve progress? | Behavioral observation, intervention control, outcome attribution, longitudinal learning |

When all three are enabled, a **harness-neutral runtime** coordinates their
contracts:

```text
                         Cognitive Runtime
          lifecycle, capability negotiation, transactions,
                    completion aggregation
                 ↙              ↓              ↘
                CFW            CSW              DP
          local authority   workflow truth   adaptive behavior
                 ↘              ↓              ↙
                        Harness adapter
                   Codex / Athena / other
                              ↓
                            Agent
                              │
                 actions, artifacts, evidence
                              └──────────→ DP outcome attribution
```

This separation is intentional. CFW and CSW each run independently, and they can
run together without DP. Enabling DP adds cross-system adaptation and independent
credit assignment; it does not transfer CFW's action authority or CSW's workflow
authority. `cognitive_runtime_core` owns generic transaction sequencing and
reversible CFW/CSW application. DP's `RuntimeHost` supplies DP-specific decision
projections and reconciles uncertain behavior-decision receipts.

A behavioral problem should not automatically become another paragraph in the
model's prompt. A workflow problem should not be solved with generic reasoning
advice. And a behavioral hypothesis should not become runtime policy before it
has been tested.

## Installation and host registries

CognitiveFrameWorks is host-neutral. Install into a built-in registry or pass any
host's skill directory explicitly:

```bash
# Built-in registries
python3 scripts/install-framework.py --target agents
python3 scripts/doctor.py --target agents

# Any third-party host registry
python3 scripts/install-framework.py \
  --registry my-host="$HOME/.my-host/skills"
python3 scripts/doctor.py \
  --registry my-host="$HOME/.my-host/skills"
```

The installer writes an ownership manifest and updates only CFW-owned files. It
atomically replaces the versioned `cognitiveframeworks_runtime` tree while
preserving the host's configuration, providers, models, agents, plugins,
sessions, memories, databases, built-in skills, and unrelated user skills.

## FreeInference Acknowledgment

This project was developed with the assistance of AI inference provided by [FreeInference.org](https://freeinference.org/).

This is an independent project. FreeInference did not review, sponsor, direct, supervise, approve, or endorse its development or contents. The project's design, implementation, claims, and maintenance are independent of FreeInference.

If you find this project useful, please consider supporting the infrastructure that helped make its development possible by donating to [FreeInference.org](https://freeinference.org/).

## Design principles

**Policy should be proportional to the task.** Do not load everything because it
exists.

**Agent action is not evidence of correctness.** Execution and verification are
different events.

**Observed contradiction reopens stale conclusions.** Previous confidence does not
outrank new evidence.

**Behavioral adaptation cannot weaken structural authority.** Learning operates
inside legal boundaries.

**Current-task authority should remain reproducible.** Structural authority stays
pinned; optional behavioral projections can change through revisioned,
host-applied overlays. Slow-loop learning remains a separate process from
uncontrolled policy mutation during a task.

**Repeated mistakes should become engineering problems.** If a failure recurs,
investigate the structure producing it rather than repeatedly reminding the model
to "be careful."

## What CFW is becoming

CognitiveFrameWorks started as a way to address recurrent model behavior. That
remains its foundation. But the larger architecture makes a stronger distinction:

```text
CognitiveFrameWorks
    HOW should the agent behave?

CognitiveStateWorks
    WHAT behavior and transitions make sense NOW?

DigitalPsychology
    WHY does this behavior recur under THESE CONDITIONS,
    and WHAT ACTUALLY CHANGES IT?
```

Together, the three systems turn agent reliability from a collection of prompt
instructions into a **measurable, state-aware, closed-loop control problem**. That
is the direction of the project.
