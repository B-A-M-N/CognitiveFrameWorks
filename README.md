# CognitiveFrameWorks

> **Cognitive components and enforcement for AI agents.**
> Compose reusable reasoning, action, evidence, recovery, and communication mechanisms for each task.

CognitiveFrameWorks (CFW) is the **cognitive component and enforcement layer** of a larger closed-loop architecture for building more reliable AI agents. DigitalPsychology owns cross-system behavioral adaptation when enabled; CFW keeps its component contracts and action authority.

It began with a simple observation: agent failures are often recurrent.

Models would repeatedly make the same classes of mistakes — acting before inspecting, overclaiming what evidence proved, widening scope, persisting with failed approaches, using tools unsafely, losing state across long sessions, or communicating conclusions with more confidence than the evidence justified.

Rather than correcting those behaviors one conversation at a time, CognitiveFrameWorks turns them into explicit, reusable cognitive mechanisms and enforceable constraints.

Over time, that grew from a collection of behavioral protocols into a host-integrated runtime capable of composing the components a task needs, enforcing structural constraints around actions, tracking evidence and authority, consuming CSW-authorized workflows, and emitting behavioral signals for later analysis.

**CFW answers one primary question:**

> **Which cognitive components and enforcement rules apply while this agent works?**

## Current implementation

CognitiveFrameWorks is implemented as task-local enforcement and a component policy compiler:

- **Standalone behavior authority** gives CFW deterministic behavior and route policy when DP is absent. The trusted host still owns the `TaskGoalContract`, its acceptance criteria, and the completion evidence.
- **Task authority** pins the goal, structural eligibility, component floors, and local enforcement policy. Revisioned behavioral overlays may adjust optional behavior without changing those authority boundaries.
- **Capability composition** supports CFW-only, CFW + CSW, CFW + DP, and the full CFW + CSW + DP deployment. A missing StateWork checkout does not prevent an ordinary CFW-only task from resolving.
- **Safe adaptive advice** is applied only after the host computes eligible choices. CFW exports a task-specific behavior contract and rejects any DigitalPsychology projection below a selected component's baseline floor. Transient operations such as `reset`, `reopen`, and `recovery` are validated separately from baseline modes. Malformed or ineligible advice is rejected as a whole.
- **Action authority** remains host-owned. `ActionStrategy` provides bounded preference hints, but every action still passes the structural action gate.
- **Evidence and telemetry** are separated: the runtime keeps local NDJSON telemetry, while the optional exporter provides bounded, cursor-based, event-ID-idempotent delivery to an external service.
- **StateWork integration** accepts only workflows eligible under CSW's current authoritative selection and delegates legal state transitions and evidence checks to CSW.

The public-beta gate is the authoritative automated release gate. It covers structural acceptance, cross-system integration, packaging, installation, upgrade, hostile-install, CSW, and DP behavior. Real-model reasoning claims still require running `scripts/acceptance-real-agent.py` with an explicitly supplied external agent command; deterministic tests do not substitute for that qualification.

## Five-minute standalone quick start

CFW does not require either sibling checkout. Install into an isolated host registry and run the checked-in example:

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

The example resolves an `implement` task, shows the host action gate decision, and prints the immutable policy hash. The `read` action is authorized by CFW itself; this is not evidence that a real model reasons better.

## Optional integrations

- **CFW alone:** CFW uses its deterministic standalone behavior authority and action gates; the host-owned `TaskGoalContract` remains authoritative for the objective and completion criteria. No CSW or DP installation is loaded.
- **CFW + CSW:** install CSW's optional CFW provider and point `--statework-root` at its registry. CSW owns workflow state, evidence, and legal transitions; CFW consumes the provider contract for compatible component context and action gates.
- **CFW + CSW + DP:** CFW and CSW keep those same authorities; the trusted DP host uses their declared ports to coordinate bounded behavioral interventions and measures outcomes using host evidence.
- **CFW + DP:** inject a `BehavioralAdvisor` backed by DP, or load a validated routing profile. For live adaptation, the trusted host selects DP's `CFWBehaviorAdapter` through the generic `RuntimeTaskHandle.create_component_adapter(factory)` port and passes it to DP's `HostControlBridge`. The host launches DP's private MCP process and applies its prepared directives through CFW's lifecycle overlay. CFW's task handle has no built-in knowledge of DP, and its hard permission gates remain active.

CFW's host convergence ledger is the source for bounded task-local attempt and recovery limits used by hard gates. CFW's task evidence ledger stores local criterion, evidence, blocker, and workflow facts. Its attention and redirect tracker is constructed only under the standalone fallback authority; a DP-bound session has no CFW attention controller. Cross-framework behavior interpretation and focus belong to DP.

Historical System-1 protocol and signal readers are isolated for old artifacts; the live CFW runtime does not instantiate them. Semantic assessment, provider state, and global behavior selection belong to DP.

The sibling projects are optional integrations, not CFW dependencies. See the sibling READMEs for their standalone entry points.

CFW-only startup does not discover or import an installed CSW provider. A host can inject `WorkflowPort` into `CognitiveRuntime` to supply eligible workflow choices and the resolved execution plan; this path keeps matching, prerequisite planning, and flow selection behind the host contract. The same contract also exposes snapshot, active-guidance, and step operations for workflow hosts. CFW validates returned task scope, pins, dependency attestation, and capsule hashes. CSW remains responsible for its registry, workflow selection, packet store, and legal transitions. The explicit-root `cognitiveframeworks.statework_providers` integration remains as a compatibility path; it still performs StateWork matching and planning inside `resolve-runtime.py` and should be removed after hosts migrate to `WorkflowPort`.

For the sibling source checkouts, install CSW into the same Python environment
that runs CFW, then pass the CSW root to the host or set
`COGNITIVE_STATEWORK_ROOT`:

```bash
python3 -m pip install -e ../CognitiveStateWork
```

This provider install and root configuration are optional; CFW remains standalone when they are absent.

| Deployment | CFW role | Other-system requirement |
| --- | --- | --- |
| **CFW alone** | Components, deterministic standalone behavior policy, evidence handling, and action gates | None; the host supplies the task contract |
| **CSW alone** | Not loaded | CSW runs through its standalone host interface |
| **CFW + DP** | Supplies signals and validates/applies component directives | DP decision service is an optional private host process |
| **CSW + DP** | Not loaded | CSW remains the authority for eligible StateWork selection and legal transitions |
| **CFW + CSW** | Task-local components and action enforcement | CSW independently owns workflow state, evidence, and legal transitions |
| **CFW + CSW + DP** | Supplies bounded signals and validates/applies component directives | DP coordinates adaptation; CSW continues to authorize workflow operations |

CFW and CSW each have standalone entry points and work together without DP.
The full three-system mode is recommended when behavioral interventions should
adapt across both cognitive components and domain workflows.

### Codex App Server adapter

Codex is one optional harness adapter. It translates App Server events into the
shared lifecycle contract; it does not define the Triad runtime or its policy.
Other harnesses use their own adapters against the same neutral contracts.

The optional CFW adapter distribution exposes `codex_app_server_adapter.py` as
a compatibility facade over the CFW task host in
`cognitive_runtime_core.integrations.codex.cfw_host`. That host connects a
host-authored CFW task to the generic Codex App Server stdio API. It registers
dynamically named tools with JSON schemas, CFW action descriptors, and trusted
executors; each call passes through CFW's action gate before the executor
runs. The host owns the task contract, stable session identity, recovery
record, validation, and completion. Codex protocol, launch, feature-catalog,
and transport implementation live in the optional runtime-core Codex
integration; `scripts/codex_host` contains compatibility imports only.
Host adapters translate trusted execution results into the neutral
`artifact_changed` observation. CFW uses that host-observed signal to reset
verification limits for evidence that changed; this policy does not inspect a
harness protocol or model-supplied result. The Codex adapter maps an explicit
boolean `artifact_changed` or `changed` field from a registered write
executor; other adapters translate their host result into the same field.
The default child configuration disables every recognized native execution,
tool, integration, and local side-effect feature that is outside the host's
registered CFW tools. It also disables MCP servers and web search while
retaining App Server infrastructure required for host-registered dynamic
tools. Codex App Server dynamic tools are experimental and require the client's
`experimentalApi` capability. See the
[Codex App Server guide](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) for the
protocol details.

The Codex transport adapter is a separate optional distribution named
`cognitive-runtime-codex-adapter`. CFW's integration is also a separate
distribution, `cognitive-frameworks-codex-adapter`; the base CFW wheel contains
no Codex host package or App Server import facades. In a workspace with
adjacent CFW and DP checkouts, install the neutral runtime, the CFW runtime,
the Codex adapter with its CFW extra, then the compatibility distribution:

```bash
python3 -m pip install ../DigitalPsychology/cognitive_runtime_core
python3 -m pip install .
python3 -m pip install '../DigitalPsychology/cognitive_runtime_core/integrations/codex[cfw]'
python3 -m pip install ./integrations/cfw-codex-adapter
```

Hosts using the shared lifecycle contracts without Codex can install only
CFW's `harness-runtime` extra. It provides the neutral `CognitiveHarnessLifecycle`
and canonical event contracts; each harness supplies its own adapter and
declared capabilities. When the lifecycle is enabled, CFW requires the Codex
identity to qualify as `FULL_CONTROL` before a trial can start. The CFW core
package can also be installed without the neutral runtime when no shared
harness lifecycle is needed.

CFW core and its neutral lifecycle integration consume host-authored task,
action, validator, and completion contracts; neither depends on a harness
protocol or authentication scheme. Optional Codex support is split across the
generic `cognitive-runtime-codex-adapter`, which translates App Server events
to neutral contracts and contains the optional CFW task host, and the
`cognitive-frameworks-codex-adapter` compatibility distribution. CFW does not
require Codex to run. The Codex adapter
launches `codex` from `PATH` by default; set `CODEX_APP_SERVER_BIN` to the App
Server executable when `codex` is a wrapper or resolves to another launcher.

For a composite CFW+CSW+DP host, pass a neutral
`CognitiveHarnessLifecycle` implementation as `runtime_harness_lifecycle`.
Codex projects App Server observations into canonical task, model, action,
validation, completion, and task-end events. The optional DP lifecycle adapter
connects those events to DP's private `HostControlBridge`; it receives the
application's CFW and CSW authority context after the CFW task starts. Its
`bridge_factory` binds the DP session to that exact task, and candidate and
evidence projectors receive bounded shared contracts. The composition root
supplies its selected CFW component adapter factory to the DP lifecycle; the
DP integration does not choose a CFW implementation. Raw App Server events and
action arguments stay in the Codex adapter. DP control tools remain
host-private; the App Server receives only explicitly registered host actions.
The `cfw_` names used in fixtures are conventional labels, not a runtime
requirement.
Visibility status reports zero model-visible DP tools only when the adapter's
own constrained App Server launcher (or a host-verified client) is used.
The adapter status also includes a bounded runtime lifecycle snapshot:
private DP bridge readiness, CFW authority verification, shared CSW workflow
bindings, CFW/DP behavior revisions, intervention count, and DP's aggregate
production counters and timing data. This lets an operator distinguish a
completed Codex turn from a task that actually ran through the triad.
Use the adapter's `task_start_options` to pass host-owned CFW authority objects
such as the shared CSW-backed `state_authority`; the adapter reserves session
identity and resume fields so these objects cannot replace recovery identity.
The DP connector requires `combined_enforcing` mode and non-empty CFW workflow
bindings by default. `require_shared_csw=False` is an explicit CFW+DP-only mode.
Combined startup keeps behavior authority pending until the host-selected DP
adapter binds. The host must run the initial DP task-start boundary and apply
its projection before model or planning context is requested; until then those
boundaries fail closed and CFW blocks action proposals.

This is a direct integration with the regular packaged Codex CLI and its generic
App Server protocol. It is independent of HarvardCodex Control. The host pins
the CLI version and complete feature catalog and refuses startup for an
unreviewed catalog. Provider credentials come from `--provider-env-file`, then
`CODEX_PROVIDER_ENV_FILE`, then the explicitly named environment variable; no
machine-specific credential path is assumed. The native model action surfaces
remain disabled, and only host-registered tools are available to the model.
Tool names are selected by the host and need only satisfy the adapter's bounded
identifier rules.

Recorded ChatGPT-mode attempts, including a user-approved preflight-only recheck
on 2026-10-07, stopped during App Server workspace-routing discovery before the
host, triad, or model turn started. The completed external-provider
sample used FreeInference's Responses endpoint. This is a provider-path
qualification for regular Codex; it does not change the host boundary or imply
that the ChatGPT routing failure was caused by CFW.

On the four-defect coding fixture, the recorded baseline reached 4/4 in all ten
runs, so the fixture has no baseline headroom for a behavioral-superiority
claim. The 50-trial dirty-tree matrix recorded CFW at 7/10 full scores and
CFW+CSW, triad, and triad-multipath at 10/10 each. These results establish
integration and sample-specific non-regression only; they do not establish
general efficacy. The [live-results index](evaluation_fixtures/live-results/README.md)
records the exact artifacts and limitations.

The matched coding evaluation records a canonical runtime cohort identity on
each trial, including model/provider pins, task and binding digests, source
snapshots for CFW/CSW/DP, the Codex feature catalog, and Laya configuration.
Paired efficacy deltas are counted only when both trials have valid, identical
cohort hashes. Missing, malformed, or different identities remain visible as
excluded pair statuses and cannot support a same-cohort causal claim.
Trial summaries classify outcomes as `PASS`, `FAIL`, `INVALID_TREATMENT`,
`INFRASTRUCTURE_BLOCKED`, `CAPABILITY_BLOCKED`, or `INSUFFICIENT_EVIDENCE`;
matched-pair reporting adds `INCOMPARABLE_COHORT` when identity hashes differ.

Completion validators use `CodexValidatorBinding` entries keyed to host-owned
acceptance criteria. Each entry fixes the action binding and arguments, declares
the criteria and boundary evidence it covers, and validates output against a
host-authored JSON Schema. `validate_completion()` aggregates validator
evidence with typed completion receipts from CFW and the host-selected runtime
authorities. Combined mode requires both DP and CSW receipts; the runtime then
rechecks its completion permit and current workflow state before finalizing.
Missing, blocked, or failed validators or authorities leave the task incomplete;
the model cannot supply a pass boolean or invent evidence.

The Codex adapter emits the neutral `CognitiveHarnessLifecycle` event contract
for task, model, action, validation, and completion boundaries. It pins its
declared capabilities to the reviewed Codex feature catalog; an unverified
host version or catalog degrades those boundaries explicitly. Codex has no
separate cognitive-host callback contract: it only translates App Server
events into the shared lifecycle, while the runtime connects CFW, CSW, and DP
through their neutral ports.

The deterministic adapter contract fixture is in
`evaluation_fixtures/codex-app-server-host-v1.json`. It runs matched baseline,
treatment, and safe-read holdout cases. It checks host gate behavior and tool
correlation; it does not evaluate model effectiveness or support efficacy claims.

For a live, narrowly scoped action-gate comparison, run
`python integration_tests/run_codex_app_server_live_evaluation.py --trials 10`.
It uses the regular Codex App Server profile, an identical disposable repository
and `cfw_write` tool in each condition, and counterbalances baseline, CFW-only,
CFW+CSW, and CFW+CSW+DP in seeded random order. The JSON result is written under
`evaluation_fixtures/live-results/` after each trial. This experiment measures
only whether each host boundary blocks this fixture's protected write; it is not
a general coding-agent effectiveness claim.

## The larger architecture

CognitiveFrameWorks is one of three complementary systems:

| System | Primary question | Role |
| --- | --- | --- |
| **CognitiveFrameWorks** | **Which cognitive mechanisms and guards are available?** | Reusable reasoning components, execution discipline, authority, evidence, recovery, communication |
| **CognitiveStateWorks** | **Which workflow states and transitions are legal now?** | Domain state, workflow legality, transition requirements, handoffs, recovery |
| **DigitalPsychology** | **How should behavior adapt, and did the intervention improve progress?** | Behavioral observation, intervention control, outcome attribution, and longitudinal learning |

When all three are enabled, a **harness-neutral runtime** coordinates their
contracts. Harness adapters translate host events into the shared lifecycle;
CFW, CSW, and DP consume those neutral contracts and do not depend on Codex or
another agent host:

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

This separation is intentional.

CFW and CSW each run independently, and they can run together without DP.
Enabling DP adds cross-system adaptation and independent credit assignment;
it does not transfer CFW's action authority or CSW's workflow authority. In
the target architecture, the neutral runtime owns host orchestration while
Codex and other harnesses remain replaceable adapters around the same triad
contracts. `cognitive_runtime_core` now owns generic transaction sequencing
and reversible CFW/CSW application. DP's `RuntimeHost` supplies DP-specific
decision projections and reconciles uncertain behavior-decision receipts.

A behavioral problem should not automatically become another paragraph in the model's prompt. A workflow problem should not be solved with generic reasoning advice. And a behavioral hypothesis should not become runtime policy before it has been tested.

## What CognitiveFrameWorks governs

CFW owns reusable cognitive components and their enforcement contracts. DP owns the cross-component behavioral state and adaptation policy. In combined mode, DP supplies global semantic interpretation and behavior selection while CFW keeps its local gates. CFW rejects provider injection and no longer carries semantic-provider policy in task bundles; configure semantic assessment in DP. Standalone CFW uses its deterministic fallback over host facts. Action accounting follows the host descriptor or objective binding; semantic output cannot relabel it.

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

At runtime, a task is resolved into a pinned structural authority contract.
Optional behavior can change through revisioned host-applied overlays, subject
to CFW's component floors and action gates.

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

The goal contract is hashed into `EffectivePolicy`, the pinned
task snapshot, routing context, and task manifest. A compact form leads every
model context, and completion claims bind to its hash and the criterion IDs
they prove. Hosts should pass the user's objective and acceptance criteria in
`TaskRequest.goal_contract`. Legacy requests can still run, but successful
completion is blocked until the host pins a goal contract with at least one
required criterion and supplies host-attested evidence for each required claim.

The CFW criterion ledger supports the full bounded task set (up to 256
criteria). In a three-system runtime, DP may grant a short attention lease for
up to three criteria; CFW projects that active window while the host retains
and verifies the complete goal contract. Criteria outside the current window
remain in CFW's ledger and the host's completion check.

Lifecycle stages use their declared frequencies: per-action wrappers expire
when the host completes the action, per-artifact checks expire after host
postflight processing, and response calibration expires when output rendering
finishes. Model tool capability alone does not activate action-planning stages;
the host calls the explicit planning boundary when an action is being chosen.

A key invariant is:

> **Behavioral learning may refine legal choices. It must not make an illegal choice legal.**

CFW pins its own component and enforcement policy separately from the host's
composite runtime identity. The CFW policy hash covers CFW-owned contracts;
the composite snapshot also covers the CSW registry and workflow plan, the host
goal contract, and the model, harness, and toolset. A workflow or toolset change
therefore changes the composite identity without changing the CFW policy hash
when CFW's own policy is unchanged.

DigitalPsychology can provide evidence that a particular flow, specialist, stage, or optional intervention is counterproductive in a particular context.

CFW can use that evidence to prefer or suppress an eligible route.

It cannot use behavioral adaptation to bypass authority, required evidence, mandatory StateWork transitions, or structural safety policy.

## Fast loop and slow loop

The architecture deliberately separates two timescales.

### Fast loop — current work

The trusted host coordinates current-task behavior. CFW enforces component
and action authority, CSW owns workflow legality and evidence, and DP may adjust
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

CSW owns **StateWork eligibility, selection, legal composition, domain state,
and transitions**. The trusted host or DP may request a choice from the legal
set; CSW validates and records it. CFW consumes only normalized workflow and
component requirements through the neutral `WorkflowPort`.

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

DigitalPsychology's runtime host consumes CFW's bounded signals, projects its
revisioned behavioral state into CFW model and planning contexts, and measures
the result against host and CSW evidence. CFW's `CFWBehaviorAdapter` is selected
by the trusted integration through `RuntimeTaskHandle.create_component_adapter(factory)`.
The adapter uses CFW's declared prepare/apply/synchronize/rollback APIs and
revision checks; CFW keeps its session lifecycle and validates every action
through its own authority gate. The host can run a side-effect-free
`preflight_action()` before DP evaluates a sanitized candidate, then call
`prepare_action()` as the final CFW gate after DP actuation. The final result
includes an action request ID for correlating the eventual host result. CFW's
preflight does not execute tools or consume confirmation grants. CSW selection
and transition calls use its separate revisioned workflow API.

The versioned adapter protocol (`1.2.0`) exposes CFW signals as bounded,
cursor-based batches. DP acknowledges a batch only after its control tick and
consumed cursor commit, so a restart can replay unacknowledged signals safely.
Signal envelopes retain cause and action-scope metadata. Both CFW gate stages
also return receipts bound to the CFW authority identity, adapter version,
action digest, decision, and stable reason codes; these receipts support host
audit and do not transfer action authority to DP.

Host handles live in a bounded, expiring session registry. The registry pins
each handle to its immutable execution identity, closes telemetry resources on
explicit close or eviction, and rejects a handle if its owner identity changes.
DigitalPsychology adapter rebinding compares stable CFW authority identity
instead of relying on Python object identity.

The DP projection is replaceable semantic guidance. CFW does not place DP
directive IDs, causes, or lease bookkeeping in model context. When DP is
unavailable, DP-owned local overlays expire at host action boundaries and CFW
continues enforcing its own component and action contracts. CFW remains usable
without DP, and its optional CSW provider remains absent in standalone mode.
The trusted adapter also exposes CFW's effective surviving local projection
for DP's degraded-mode guidance after DP-owned overlays expire.
The component catalog assigns each component a `model_visibility` policy:
`guidance` may enter context, while `consequence_only` and `none` stay outside
the prompt. DP-controlled prompts omit pipeline labels and internal contract
hashes; host-side gates still enforce the underlying behavior.

Validated long-run findings can later become one of two things:

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

# Any third-party host registry
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

**Current-task authority should remain reproducible.**
Structural authority stays pinned; optional behavioral projections can change
through revisioned, host-applied overlays. Slow-loop learning remains a
separate process from uncontrolled policy mutation during a task.

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

## Tests, limitations, and release status

The authoritative automated release gate is [`release-gates.json`](release-gates.json). It covers CFW runtime/public API/structural/session routing, CSW validation/control-plane/concurrency, DP self-tests/routing/cross-system, guard-policy causal acceptance, the private-MCP runtime triad, dynamic improvement, package resources, installation, upgrade, and hostile-install checks. The runtime-triad gate runs the current DP, CFW, and CSW authorities together and qualifies restart reconciliation, CFW final-gate rejection, lease ownership, and twelve-criterion attention coverage.

The public-beta gate uses the three local checkout layout. Standalone CFW behavior is separately exercised by the isolated install, doctor, and quickstart commands above.

Structural tests establish deterministic policy, authority, state, and integration behavior. They do not establish that an external model reasons differently. Real-agent claims require [`scripts/acceptance-real-agent.py`](scripts/acceptance-real-agent.py) with an explicit external agent command and complete objective qualification. Host integrations, model availability, tool permissions, and evaluator behavior remain deployment-specific limitations.

## License and contribution

These projects are released under the [MIT License](LICENSE). Contributions are welcome through repository issues and pull requests. Please include a focused regression or acceptance check for behavior changes, keep authority boundaries explicit, and do not claim model-performance improvements without the corresponding qualification evidence.

## Release qualification

Structural public-beta checks, hostile integration, real-agent behavioral
qualification, and operational performance are separate evidence classes.
See `contracts/qualifications/`; unexecuted results remain `not_run`.
`scripts/acceptance-real-agent-benchmark.py` measures task outcomes only. Its
report is explicitly ineligible as intervention evidence because it does not
assign system cohorts or verify host-applied controls. Real-agent qualification
must include host receipts and independent outcome attribution before it can
pass.
The final release gate requires every qualification marked
`mandatory_for_release` to have a `pass` result; `not_run`, `fail`, and
`blocked` results stop release.

## Concurrent-agent isolation and controlled sharing

Independent executions use a complete `ExecutionIdentity`: namespace, application instance, agent instance, session, task, attempt, and interaction. A private workflow key includes the agent identity, so reused local identifiers do not create shared state.

Agents may share a CSW subject only by presenting a pinned `shared_workflow_membership` whose namespace, application, application instance, StateWork, subject, workflow, and authorization revision match the task. Shared transitions still use compare-and-swap revisions; stale or concurrent incompatible writes require re-inspection.

DP profiles use three sharing scopes:

- `agent_private`: available only to the originating agent.
- `application_shared`: requires an authenticated scope-promotion decision.
- `cross_application`: additionally requires explicit data-sharing authorization.

Even a shared profile is not automatically adopted. Each new CFW task must list the exact profile hash in `TaskRequest.adopted_profile_hashes`; normal model, harness, role, context, and eligible-route checks still apply. Running tasks retain their pinned policy revision. `deferred` revocation takes effect at the next task boundary; `immediate` revocation through the host revocation authority blocks affected active actions.

`scripts/acceptance-concurrent-agent-isolation.py` verifies private-state separation, authorized shared-state serialization, crash recovery, and origin-distinct DP ingestion using separate processes and a shared persistent backend.

## Cognitive Cockpit

The optional read-only cockpit is available with `python3 scripts/cfw-ops.py` for the terminal view or `python3 scripts/cfw-ops.py --web` for a localhost-only browser view. It presents Overview, Workflow, Learning, Timeline, and Health data from the same sanitized operator-event stream. It never receives host session tokens, signing keys, or authority capabilities, and closing it does not terminate tasks.

## FreeInference Acknowledgement

This work benefited in some way from inference provided by [freeinference.org](https://freeinference.org/).

These are independent developments that are not reviewed, endorsed, or sponsored by FreeInference. If you find these projects useful, please consider donating to or sponsoring FreeInference, which provides a vital inference service.
