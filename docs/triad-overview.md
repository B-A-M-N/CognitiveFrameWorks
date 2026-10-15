# The Triad: three authorities, one agent

This overview is shared by all three repositories. Each project keeps its own
README; this page explains how they fit together and what each one is *not*
responsible for.

The three systems are **different authorities**, not three competing sets of
instructions. Each answers a different question about the same work, and each is
the sole owner of its own decisions.

| Project             | Plain-English question                        | Technical responsibility                                                                  |
| ------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------------- |
| CognitiveFrameWorks | *How* an agent should conduct its work        | Behavioral components, task policy, action enforcement, evidence discipline              |
| CognitiveStateWorks | *What state* the work is in, and what follows | Durable state, workflow eligibility, evidence requirements, legal transitions            |
| DigitalPsychology   | *When and why* behavior should be adjusted    | Behavioral telemetry, bounded interventions, outcome attribution, experimental learning   |

## How a single decision flows

```
                  ┌─────────────────────────────┐
   the task  ───▶ │  CognitiveStateWorks        │  What state is the work in,
                  │  eligible StateWorks        │  and which transitions are
                  │  required evidence          │  legal from here?
                  └──────────────┬──────────────┘
                                 │  the legal, evidence-backed option set
                                 ▼
                  ┌─────────────────────────────┐
                  │  CognitiveFrameWorks        │  How should this be done?
                  │  components · task policy   │  Which behavioral policy and
                  │  action gates · enforcement │  enforcement boundaries apply?
                  └──────────────┬──────────────┘
                                 │  a policy-bounded intent
                                 ▼
                  ┌─────────────────────────────┐
                  │  DigitalPsychology          │  Is the agent's behavior
                  │  telemetry · interventions  │  drifting? Should a bounded
                  │  outcome attribution        │  adjustment be proposed?
                  └──────────────┬──────────────┘
                                 │  an observation, or a bounded
                                 │  intervention request
                                 ▼
                  ┌─────────────────────────────┐
                  │  Trusted host               │  Executes only what the
                  │  (outside the agent's        │  authority boundaries permit.
                  │   model-visible tools)       │  CFW and CSW keep their own
                  └─────────────────────────────┘
```

Read it as a constraint chain: CSW narrows what is *possible*, CFW governs *how*
it is approached, DP observes and proposes *adjustments*, and the trusted host
performs only what is authorized.

## What each system does not do

- **CSW does not** decide how to behave, evaluate agent conduct, or learn from
  outcomes. It records state and enforces transition legality.
- **CFW does not** own workflow state, and does not authorize its own
  interventions. Components guide behavior; a capable host is required for
  structural enforcement.
- **DP does not** write workflow state, enforce action policy, or decide which
  StateWork is legal. It observes, attributes outcomes, and proposes bounded
  adjustments within its authority.

## Independence and optionality

All three run **independently**. CSW and CFW each work alone; DP runs alone for
behavioral analysis. Integration is optional at every layer, and no project
requires the others to function.

## Where the technical detail lives

Each repository documents only its own authority and implementation. Start with
the README in the project you care about; the `docs/` directories hold the
contracts, runtime boundaries, and qualification evidence.

## FreeInference Acknowledgment

This project was developed with the assistance of AI inference provided by [FreeInference.org](https://freeinference.org/).

This is an independent project. FreeInference did not review, sponsor, direct, supervise, approve, or endorse its development or contents. The project's design, implementation, claims, and maintenance are independent of FreeInference.

If you find this project useful, please consider supporting the infrastructure that helped make its development possible by donating to [FreeInference.org](https://freeinference.org/).
