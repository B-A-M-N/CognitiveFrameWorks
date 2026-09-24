# CognitiveFrameWorks

> **Behavioral policy and runtime governance for AI agents.**
> Define how an agent should reason, act, verify, recover, and communicate — without turning every task into a wall of instructions.

CognitiveFrameWorks (CFW) is the **behavioral control layer** of a larger closed-loop architecture for building more reliable AI agents.

It began with a simple observation: agent failures are often recurrent.

Models would repeatedly make the same classes of mistakes — acting before inspecting, overclaiming what evidence proved, widening scope, persisting with failed approaches, using tools unsafely, losing state across long sessions, or communicating conclusions with more confidence than the evidence justified.

Rather than correcting those behaviors one conversation at a time, CognitiveFrameWorks turns them into explicit, reusable behavioral policy.

Over time, that grew from a collection of behavioral protocols into a host-integrated runtime capable of composing only the policies a task actually needs, enforcing structural constraints around actions, tracking evidence and authority, coordinating with domain StateWorks, and emitting behavioral telemetry for later analysis.

**CFW answers one primary question:**

> **How should this agent behave while doing the work?**

## The larger architecture

CognitiveFrameWorks is one of three complementary systems:

| System | Primary question | Role |
| --- | --- | --- |
| **CognitiveFrameWorks** | **How should the agent behave?** | Reasoning policy, execution discipline, authority, evidence, recovery, communication |
| **CognitiveStateWorks** | **What behavior and transitions are appropriate now?** | Domain state, workflow legality, transition requirements, handoffs, recovery |
| **DigitalPsychology** | **Why is the agent behaving this way under these conditions?** | Behavioral observation, trajectory analysis, experimentation, longitudinal learning |

Together they form a **closed-loop cognitive-behavioral control architecture**:

```text
                           ┌──────────────────────────────┐
                           │     DigitalPsychology       │
                           │                              │
                           │ observe → characterize       │
                           │ compare → experiment         │
                           │ validate → monitor           │
                           └──────────────┬───────────────┘
                                          │
                            validated behavioral findings
                                          │
                       ┌──────────────────┴──────────────────┐
                       │                                     │
                       ▼                                     ▼
             CognitiveFrameWorks                    CognitiveStateWorks
             behavioral policy                      operational state
             reasoning & action                     legal transitions
             evidence & authority                   evidence requirements
                       │                                     │
                       └──────────────────┬──────────────────┘
                                          │
                                          ▼
                                        Agent
                                          │
                               actions / results / state
                                          │
                                          └──── telemetry ───→
```

This separation is intentional.

A behavioral problem should not automatically become another paragraph in the model's prompt. A workflow problem should not be solved with generic reasoning advice. And a behavioral hypothesis should not become runtime policy before it has been tested.

## What CognitiveFrameWorks governs

CFW owns the **prescriptive behavioral layer**.

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

CFW does **not** try to encode every domain workflow.

That belongs to CognitiveStateWorks.

CFW also does not decide that a behavioral tendency exists merely because an agent or operator says so.

That belongs to DigitalPsychology.

## The behavioral framework

CognitiveFrameWorks currently contains seven core behavioral systems.

| Component | Responsibility |
| --- | --- |
| **OWL** | Reasoning discipline, epistemics, locality, conservation, verification |
| **ANCHOR** | Operational continuity, recovery, state integrity, completion discipline |
| **DOX** | Documentation and repository-contract hierarchy |
| **FUSE** | Tool selection, sequencing, evidence interpretation, retry/termination discipline |
| **WARD** | Authority, trust boundaries, destructive actions, secrets, reversibility |
| **FLOW** | Operational drag, scalability, retries, backpressure, hot paths, maintenance weight |
| **SISPIS** | Communication and output calibration |

These are not intended to become seven giant prompt blocks loaded on every request.

CFW uses **progressive disclosure and task composition**: activate the smallest legal behavioral surface that serves the task.

## Runtime model

At runtime, a task is resolved into a frozen effective policy.

```text
TaskRequest
    │
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
 Optional validated behavioral routing
        from DigitalPsychology
            │
            ▼
      EffectivePolicy
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

A key invariant is:

> **Behavioral learning may refine legal choices. It must not make an illegal choice legal.**

DigitalPsychology may eventually provide evidence that a particular flow, specialist, stage, or optional intervention is counterproductive in a particular context.

CFW can use that evidence to prefer or suppress an eligible route.

It cannot use behavioral adaptation to bypass authority, required evidence, mandatory StateWork transitions, or structural safety policy.

## Fast loop and slow loop

The architecture deliberately separates two timescales.

### Fast loop — current work

CFW and CognitiveStateWorks govern the running task:

```text
observe
→ reason
→ act
→ receive evidence
→ update operational state
→ continue / recover / complete
```

Current-task policy is frozen so that execution remains reproducible and causal attribution remains meaningful.

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

That allows the system to improve future behavior **without continuously rewriting the rules underneath a task that is already running**.

## Session-aware telemetry

CFW emits structured behavioral telemetry without requiring DigitalPsychology itself to be loaded into the agent.

Runtime identity distinguishes:

```text
agent instance
    └── session
         └── task
              └── attempt
                   └── execution segment
                        └── event
```

Delegated and multi-agent work may additionally carry interaction, parent-session, delegator, delegate, role, and delegation identities.

This prevents unrelated sessions from being accidentally collapsed into one behavioral history.

The runtime's security-sensitive authority identities remain separate from analytics identities.

## CognitiveStateWorks integration

CFW determines **which StateWork is applicable and how it is composed**.

CognitiveStateWorks determines **which domain states and transitions are legal**.

For example:

```text
CFW:
"this task requires infrastructure reasoning and mutation controls"

        ↓

Infrae StateWork:
UNKNOWN
→ OBSERVED
→ MODELED
→ ASSESSED
→ PLANNED
→ CHANGING
→ VERIFIED
→ STABLE
```

CFW cannot simply declare the infrastructure stable because the agent says the deployment succeeded.

The StateWork transition contract determines what evidence is required to reach that state.

## DigitalPsychology integration

DigitalPsychology consumes CFW's behavioral telemetry and evaluates actual behavior across tasks and sessions.

Its validated output may eventually become one of two things:

### Structural behavioral guards

A repeatedly demonstrated failure can become a small runtime constraint.

Example:

```text
completion claim
+ unresolved contradictory observation
→ completion remains illegal
```

### Behavioral routing policy

A repeatedly demonstrated context-specific routing failure can influence future composition.

Example:

```text
agent/model/harness/task-family combination
repeatedly activates an unnecessary specialist
without improving task outcomes
        ↓
validated experiment
        ↓
future eligible tasks suppress that optional specialist
```

The goal is not endlessly accumulating rules.

The goal is for the active policy surface to become **more precise and, where evidence permits, smaller**.

## Installation and host registries

CognitiveFrameWorks is host-neutral. Install into a built-in registry or pass any host's skill directory explicitly:

```bash
# Built-in registries
python3 scripts/install-framework.py --target agents
python3 scripts/doctor.py --target agents

# Any host registry, including HarvardCodex-like layouts
python3 scripts/install-framework.py \
  --registry my-host="$HOME/.my-host/skills"
python3 scripts/doctor.py \
  --registry my-host="$HOME/.my-host/skills"
```

The installer writes an ownership manifest and updates only CFW-owned files. It atomically replaces the versioned `cognitiveframeworks_runtime` tree while preserving the host's configuration, providers, models, agents, plugins, sessions, memories, databases, built-in skills, and unrelated user skills.

## Validation

Core validation surfaces include:

```bash
python3 scripts/validate-framework.py
python3 scripts/test-runtime.py
python3 scripts/acceptance-public-beta.py
python3 scripts/acceptance-session-routing.py
python3 scripts/acceptance-clean-install.py
python3 scripts/acceptance-upgrade-install.py
python3 scripts/doctor.py --registry my-host="$HOME/.my-host/skills"
```

Different tests establish different claims.

A deterministic structural acceptance test can prove that a host gate blocks an illegal action.

It cannot prove that a real model reasons differently.

Behavioral claims therefore require real-agent qualification in addition to deterministic runtime tests.

## Design principles

**Policy should be proportional to the task.**
Do not load everything because it exists.

**Agent action is not evidence of correctness.**
Execution and verification are different events.

**Observed contradiction reopens stale conclusions.**
Previous confidence does not outrank new evidence.

**Behavioral adaptation cannot weaken structural authority.**
Learning operates inside legal boundaries.

**Current-task policy should remain reproducible.**
Learning belongs primarily between sessions, not as uncontrolled mutation during one.

**Repeated mistakes should become engineering problems.**
If a failure recurs, investigate the structure producing it rather than repeatedly reminding the model to "be careful."

## What CFW is becoming

CognitiveFrameWorks started as a way to address recurrent model behavior.

That remains its foundation.

But the larger architecture makes a stronger distinction:

```text
CognitiveFrameWorks
    HOW should the agent behave?

CognitiveStateWorks
    WHAT behavior and transitions make sense NOW?

DigitalPsychology
    WHY does this behavior recur under THESE CONDITIONS,
    and WHAT ACTUALLY CHANGES IT?
```

Together, the three systems turn agent reliability from a collection of prompt instructions into a **measurable, state-aware, closed-loop control problem**.

That is the direction of the project.

## Current implementation

CognitiveFrameWorks is implemented as a host-owned runtime and policy compiler:

- **Task policy resolution** freezes an immutable policy snapshot for each task.
- **Capability composition** supports CFW-only, CFW + CSW, and CFW + DP deployments. A missing StateWork checkout does not prevent an ordinary CFW-only task from resolving.
- **Safe adaptive advice** is applied only after the host computes eligible choices. Mandatory stages such as `WARD` cannot be suppressed, and malformed or ineligible advice is rejected as a whole.
- **Action authority** remains host-owned. `ActionStrategy` provides bounded preference hints, but every action still passes the structural action gate.
- **Evidence and telemetry** are separated: the runtime keeps local NDJSON telemetry, while the optional exporter provides bounded, cursor-based, event-ID-idempotent delivery to an external service.
- **StateWork integration** validates explicit and learned flows against the same operation, trigger, shape, and current-state selectors.

The public-beta gate is the authoritative automated release gate. It covers structural acceptance, cross-system integration, packaging, installation, upgrade, hostile-install, CSW, and DP behavior. Real-model reasoning claims still require running `scripts/acceptance-real-agent.py` with an explicitly supplied external agent command; deterministic tests do not substitute for that qualification.


## FreeInference attribution
This work benefited in some way from inference provided by [freeinference.org](https://freeinference.org/).

These are independent developments that are not reviewed, endorsed, or sponsored by FreeInference. If you find these projects genuinely useful, please consider donating to or sponsoring FreeInference, which provides a vital inference service.
