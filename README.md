# CognitiveFrameWorks

> **Reusable behavioral components and enforcement rules for AI agents.**

## What problem does this solve?

Agent failures are often recurrent. Models repeatedly act before inspecting,
overclaim what evidence proved, widen scope, persist with failed approaches, use
tools unsafely, lose state across long sessions, or communicate conclusions with
more confidence than the evidence justified.

Correcting those behaviors one conversation at a time does not scale.
CognitiveFrameWorks turns them into explicit, reusable cognitive mechanisms and
enforceable constraints.

## What does it actually do?

CognitiveFrameWorks provides reusable behavioral components and enforcement rules
for AI agents. It helps agents reason about evidence, select appropriate tools,
maintain task continuity, respect authority boundaries, recover from failures, and
avoid unnecessary operational complexity.

Components are selected according to the task rather than loading every framework
into every interaction. A trusted host can enforce action policies independently
of the model's willingness to follow instructions.

CFW evaluates observable computational behavior, **not** consciousness, feelings,
or human psychological conditions.

### CFW answers one primary question

> **Which cognitive components and enforcement rules apply while this agent works?**

## What does it not do?

- CFW does **not** encode domain workflows. That authority belongs to
  CognitiveStateWorks.
- CFW does **not** decide that a behavioral tendency exists merely because an
  agent or operator says so. That evaluation belongs to DigitalPsychology.
- CFW does **not** grant behavioral adaptation the power to bypass authority,
  required evidence, or mandatory StateWork transitions.

## Instructions versus enforcement

Not every CFW component is an absolute control mechanism. Components such as OWL
or SISPIS guide model behavior and depend on the model following them. Others —
notably WARD and the action gate — are **structurally enforced** by a trusted host,
which blocks an action regardless of what the model intends.

Which class a given safeguard belongs to, and what that means for a deployment, is
in [`docs/enforcement.md`](docs/enforcement.md).

## The seven components

| Component | Reader-friendly description                                                |
| --------- | -------------------------------------------------------------------------- |
| **OWL**   | Examine assumptions and reasoning before acting                            |
| **ANCHOR** | Preserve task context, progress, and recovery state                        |
| **DOX**   | Follow the repository's documentation and instruction contracts            |
| **FUSE**  | Decide when and how tools should be used                                   |
| **WARD**  | Enforce permissions, safety boundaries, and mutation constraints           |
| **FLOW**  | Identify avoidable complexity, operational friction, and performance problems |
| **SISPIS** | Adjust response structure and detail to the decision being made            |

Identifiers are unchanged for compatibility. Full technical responsibilities and
model-visibility policies are in [`docs/architecture.md`](docs/architecture.md).

## How can I use it independently?

CFW does not require either sibling checkout.

```bash
python3 scripts/install-framework.py \
  --registry demo="$HOME/.demo-cfw" \
  --statework-root "$HOME/no-cognitive-stateworks-here"
python3 scripts/doctor.py --registry demo="$HOME/.demo-cfw"
python3 scripts/quickstart-standalone.py
```

Expected output includes:

```text
kernel: owl -> anchor_open -> fuse -> ward -> anchor_closeout -> sispis
read action gate: allow
policy snapshot: <64-character SHA-256>
```

The example resolves an `implement` task, shows the host action gate decision, and
prints the immutable policy hash. The `read` action is authorized by CFW itself;
**this is not evidence that a real model reasons better.**

## How does it integrate with the other two?

| Deployment       | CFW role                                                     | Other-system requirement |
| ---------------- | ------------------------------------------------------------ | ------------------------ |
| **CFW alone**    | Components, deterministic standalone policy, action gates    | None; the host supplies the task contract |
| **CSW alone**    | Not loaded                                                   | CSW runs through its standalone host interface |
| **CFW + DP**     | Supplies signals and validates/applies component directives  | DP decision service is an optional private host process |
| **CSW + DP**     | Not loaded                                                   | CSW remains the authority for eligible StateWork selection and legal transitions |
| **CFW + CSW**    | Task-local components and action enforcement                 | CSW independently owns workflow state, evidence, and legal transitions |
| **CFW + CSW + DP** | Supplies bounded signals and validates/applies directives  | DP coordinates adaptation; CSW continues to authorize workflow operations |

CFW and CSW each have standalone entry points and work together without DP. The
full three-system mode is recommended when behavioral interventions should adapt
across both cognitive components and domain workflows.

When all three are enabled, a harness-neutral runtime coordinates their contracts.
See [`docs/integrations.md`](docs/integrations.md) for adapter details, the
`WorkflowPort` contract, and shared-state rules.

## What has been demonstrated, and what remains experimental?

> **CFW provides testable behavioral policies and structural enforcement. Passing
> structural tests does not, by itself, demonstrate improvements in model
> reasoning or task success.**

Demonstrated: deterministic policy, authority, state, and integration behavior,
including Codex adapter integration and non-regression on the recorded fixtures.

Not established: that CFW generally makes agents more capable. The four-defect
coding fixture baseline already reached the fixture ceiling, leaving no headroom
for a superiority claim.

## Where is the technical documentation?

| Document                                                        | Responsibility                                                       |
| --------------------------------------------------------------- | -------------------------------------------------------------------- |
| [`docs/architecture.md`](docs/architecture.md)                  | Task policy, authority, lifecycle, composition                        |
| [`docs/integrations.md`](docs/integrations.md)                  | Host adapters and optional DP/CSW integration                        |
| [`docs/enforcement.md`](docs/enforcement.md)                    | Action gates, component floors, evidence boundaries                   |
| [`docs/qualification.md`](docs/qualification.md)                | Real-agent benchmarks, cohort experiments, release qualification     |
| [`shared/integration.md`](shared/integration.md)                | Canonical machine-facing integration contract (unchanged)            |
| [`CLAUDE.md`](CLAUDE.md)                                        | Agent-facing operational instructions (unchanged)                    |
| [`docs/triad-overview.md`](docs/triad-overview.md) | How the three authorities fit together           |

## Validation

```bash
python3 scripts/validate-framework.py
python3 scripts/test-runtime.py
python3 scripts/acceptance-public-beta.py
```

A deterministic structural acceptance test can prove that a host gate blocks an
illegal action. It cannot prove that a real model reasons differently. See
[`docs/qualification.md`](docs/qualification.md).

## Support and limits

Linux is the supported platform; the repository does not claim Windows support.
The public-beta gate uses the three local checkout layout; standalone CFW behavior
is separately exercised by the isolated install, doctor, and quickstart commands
above.

## Cognitive Cockpit

The optional read-only cockpit is available with `python3 scripts/cfw-ops.py` for
the terminal view or `python3 scripts/cfw-ops.py --web` for a localhost-only
browser view. It presents Overview, Workflow, Learning, Timeline, and Health data
from the same sanitized operator-event stream. It never receives host session
tokens, signing keys, or authority capabilities, and closing it does not terminate
tasks.

## License and contribution

These projects are released under the [MIT License](LICENSE). Contributions are
welcome through repository issues and pull requests. Please include a focused
regression or acceptance check for behavior changes, keep authority boundaries
explicit, and do not claim model-performance improvements without the
corresponding qualification evidence.

## FreeInference Acknowledgment

This project was developed with the assistance of AI inference provided by [FreeInference.org](https://freeinference.org/).

This is an independent project. FreeInference did not review, sponsor, direct, supervise, approve, or endorse its development or contents. The project's design, implementation, claims, and maintenance are independent of FreeInference.

If you find this project useful, please consider supporting the infrastructure that helped make its development possible by donating to [FreeInference.org](https://freeinference.org/).
