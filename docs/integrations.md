# Integrations

Host adapters and optional DP/CSW integration for CognitiveFrameWorks.

## Host adapters and the neutral runtime

CFW core and its neutral lifecycle integration consume host-authored task,
action, validator, and completion contracts; neither depends on a harness
protocol or authentication scheme. Harness adapters translate host events into
the shared lifecycle, and CFW consumes the resulting neutral contracts.

### Codex App Server adapter

Codex is one optional harness adapter. It translates App Server events into the
shared lifecycle contract; it does not define the Triad runtime or its policy.
Other harnesses use their own adapters against the same neutral contracts.

The optional CFW adapter distribution exposes `codex_app_server_adapter.py` as a
compatibility facade over the CFW task host in
`cognitive_runtime_core.integrations.codex.cfw_host`. That host connects a
host-authored CFW task to the generic Codex App Server stdio API. It registers
dynamically named tools with JSON schemas, CFW action descriptors, and trusted
executors; each call passes through CFW's action gate before the executor runs.
The host owns the task contract, stable session identity, recovery record,
validation, and completion. Codex protocol, launch, feature-catalog, and
transport implementation live in the optional runtime-core Codex integration;
`scripts/codex_host` contains compatibility imports only.

Host adapters translate trusted execution results into the neutral
`artifact_changed` observation. CFW uses that host-observed signal to reset
verification limits for evidence that changed; this policy does not inspect a
harness protocol or model-supplied result. The Codex adapter maps an explicit
boolean `artifact_changed` or `changed` field from a registered write executor;
other adapters translate their host result into the same field.

The default child configuration disables every recognized native execution, tool,
integration, and local side-effect feature that is outside the host's registered
CFW tools. It also disables MCP servers and web search while retaining App Server
infrastructure required for host-registered dynamic tools. Codex App Server
dynamic tools are experimental and require the client's `experimentalApi`
capability. See the
[Codex App Server guide](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)
for the protocol details.

### Adapter distributions

The Codex transport adapter is a separate optional distribution named
`cognitive-runtime-codex-adapter`. CFW's integration is also a separate
distribution, `cognitive-frameworks-codex-adapter`; the base CFW wheel contains
no Codex host package or App Server import facades. In a workspace with adjacent
CFW and DP checkouts, install the neutral runtime, the CFW runtime, the Codex
adapter with its CFW extra, then the compatibility distribution:

```bash
python3 -m pip install ../DigitalPsychology/cognitive_runtime_core
python3 -m pip install .
python3 -m pip install '../DigitalPsychology/cognitive_runtime_core/integrations/codex[cfw]'
python3 -m pip install ./integrations/cfw-codex-adapter
```

Hosts using the shared lifecycle contracts without Codex can install only CFW's
`harness-runtime` extra. It provides the neutral `CognitiveHarnessLifecycle` and
canonical event contracts; each harness supplies its own adapter and declared
capabilities. When the lifecycle is enabled, CFW requires the Codex identity to
qualify as `FULL_CONTROL` before a trial can start. The CFW core package can also
be installed without the neutral runtime when no shared harness lifecycle is
needed.

CFW does not require Codex to run. The Codex adapter launches `codex` from `PATH`
by default; set `CODEX_APP_SERVER_BIN` to the App Server executable when `codex`
is a wrapper or resolves to another launcher.

### Composite CFW+CSW+DP host

For a composite host, pass a neutral `CognitiveHarnessLifecycle` implementation
as `runtime_harness_lifecycle`. Codex projects App Server observations into
canonical task, model, action, validation, completion, and task-end events. The
optional DP lifecycle adapter connects those events to DP's private
`HostControlBridge`; it receives the application's CFW and CSW authority context
after the CFW task starts. Its `bridge_factory` binds the DP session to that exact
task, and candidate and evidence projectors receive bounded shared contracts. The
composition root supplies its selected CFW component adapter factory to the DP
lifecycle; the DP integration does not choose a CFW implementation. Raw App
Server events and action arguments stay in the Codex adapter. DP control tools
remain host-private; the App Server receives only explicitly registered host
actions. The `cfw_` names used in fixtures are conventional labels, not a runtime
requirement.

Visibility status reports zero model-visible DP tools only when the adapter's own
constrained App Server launcher (or a host-verified client) is used. The adapter
status also includes a bounded runtime lifecycle snapshot: private DP bridge
readiness, CFW authority verification, shared CSW workflow bindings, CFW/DP
behavior revisions, intervention count, and DP's aggregate production counters and
timing data. This lets an operator distinguish a completed Codex turn from a task
that actually ran through the triad.

Use the adapter's `task_start_options` to pass host-owned CFW authority objects
such as the shared CSW-backed `state_authority`; the adapter reserves session
identity and resume fields so these objects cannot replace recovery identity. The
DP connector requires `combined_enforcing` mode and non-empty CFW workflow
bindings by default. `require_shared_csw=False` is an explicit CFW+DP-only mode.
Combined startup keeps behavior authority pending until the host-selected DP
adapter binds. The host must run the initial DP task-start boundary and apply its
projection before model or planning context is requested; until then those
boundaries fail closed and CFW blocks action proposals.

This is a direct integration with the regular packaged Codex CLI and its generic
App Server protocol. It is independent of HarvardCodex Control. The host pins the
CLI version and complete feature catalog and refuses startup for an unreviewed
catalog. Provider credentials come from `--provider-env-file`, then
`CODEX_PROVIDER_ENV_FILE`, then the explicitly named environment variable; no
machine-specific credential path is assumed. The native model action surfaces
remain disabled, and only host-registered tools are available to the model. Tool
names are selected by the host and need only satisfy the adapter's bounded
identifier rules.

Recorded ChatGPT-mode attempts, including a user-approved preflight-only recheck
on 2026-10-07, stopped during App Server workspace-routing discovery before the
host, triad, or model turn started. The completed external-provider sample used
FreeInference's Responses endpoint. This is a provider-path qualification for
regular Codex; it does not change the host boundary or imply that the ChatGPT
routing failure was caused by CFW.

## CognitiveStateWorks integration

CSW owns **StateWork eligibility, selection, legal composition, domain state, and
transitions**. The trusted host or DP may request a choice from the legal set; CSW
validates and records it. CFW consumes only normalized workflow and component
requirements through the neutral `WorkflowPort`.

For example:

```text
CFW: "this task requires infrastructure reasoning and mutation controls"

        ↓

Infrae StateWork:
UNKNOWN → OBSERVED → MODELED → ASSESSED → PLANNED → CHANGING → VERIFIED → STABLE
```

CFW cannot simply declare the infrastructure stable because the agent says the
deployment succeeded. The StateWork transition contract determines what evidence
is required to reach that state.

CFW-only startup does not discover or import an installed CSW provider. A host can
inject `WorkflowPort` into `CognitiveRuntime` to supply eligible workflow choices
and the resolved execution plan; this path keeps matching, prerequisite planning,
and flow selection behind the host contract. The same contract also exposes
snapshot, active-guidance, and step operations for workflow hosts. CFW validates
returned task scope, pins, dependency attestation, and capsule hashes. CSW remains
responsible for its registry, workflow selection, packet store, and legal
transitions. The explicit-root `cognitiveframeworks.statework_providers`
integration remains as a compatibility path; it still performs StateWork matching
and planning inside `resolve-runtime.py` and should be removed after hosts migrate
to `WorkflowPort`.

For the sibling source checkouts, install CSW into the same Python environment
that runs CFW, then pass the CSW root to the host or set
`COGNITIVE_STATEWORK_ROOT`:

```bash
python3 -m pip install -e ../CognitiveStateWork
```

This provider install and root configuration are optional; CFW remains standalone
when they are absent.

## DigitalPsychology integration

DigitalPsychology's runtime host consumes CFW's bounded signals, projects its
revisioned behavioral state into CFW model and planning contexts, and measures the
result against host and CSW evidence. CFW's `CFWBehaviorAdapter` is selected by the
trusted integration through
`RuntimeTaskHandle.create_component_adapter(factory)`. The adapter uses CFW's
declared prepare/apply/synchronize/rollback APIs and revision checks; CFW keeps
its session lifecycle and validates every action through its own authority gate.
The host can run a side-effect-free `preflight_action()` before DP evaluates a
sanitized candidate, then call `prepare_action()` as the final CFW gate after DP
actuation. The final result includes an action request ID for correlating the
eventual host result. CFW's preflight does not execute tools or consume
confirmation grants. CSW selection and transition calls use its separate
revisioned workflow API.

The versioned adapter protocol (`1.2.0`) exposes CFW signals as bounded,
cursor-based batches. DP acknowledges a batch only after its control tick and
consumed cursor commit, so a restart can replay unacknowledged signals safely.
Signal envelopes retain cause and action-scope metadata. Both CFW gate stages also
return receipts bound to the CFW authority identity, adapter version, action
digest, decision, and stable reason codes; these receipts support host audit and
do not transfer action authority to DP.

Host handles live in a bounded, expiring session registry. The registry pins each
handle to its immutable execution identity, closes telemetry resources on explicit
close or eviction, and rejects a handle if its owner identity changes.
DigitalPsychology adapter rebinding compares stable CFW authority identity instead
of relying on Python object identity.

The DP projection is replaceable semantic guidance. CFW does not place DP
directive IDs, causes, or lease bookkeeping in model context. When DP is
unavailable, DP-owned local overlays expire at host action boundaries and CFW
continues enforcing its own component and action contracts. CFW remains usable
without DP, and its optional CSW provider remains absent in standalone mode. The
trusted adapter also exposes CFW's effective surviving local projection for DP's
degraded-mode guidance after DP-owned overlays expire.

Validated long-run findings can later become one of two things.

### Structural behavioral guards

A repeatedly demonstrated failure can become a small runtime constraint.

```text
completion claim
+ unresolved contradictory observation
→ completion remains illegal
```

### Behavioral routing policy

```text
agent/model/harness/task-family combination
repeatedly activates an unnecessary specialist
without improving task outcomes
        ↓
validated experiment
        ↓
future eligible tasks suppress that optional specialist
```

The goal is not endlessly accumulating rules. The goal is for the active policy
surface to become **more precise and, where evidence permits, smaller**.

## Concurrent-agent isolation and controlled sharing

Independent executions use a complete `ExecutionIdentity`: namespace, application
instance, agent instance, session, task, attempt, and interaction. A private
workflow key includes the agent identity, so reused local identifiers do not
create shared state.

Agents may share a CSW subject only by presenting a pinned
`shared_workflow_membership` whose namespace, application, application instance,
StateWork, subject, workflow, and authorization revision match the task. Shared
transitions still use compare-and-swap revisions; stale or concurrent incompatible
writes require re-inspection.

DP profiles use three sharing scopes:

- `agent_private`: available only to the originating agent.
- `application_shared`: requires an authenticated scope-promotion decision.
- `cross_application`: additionally requires explicit data-sharing authorization.

Even a shared profile is not automatically adopted. Each new CFW task must list
the exact profile hash in `TaskRequest.adopted_profile_hashes`; normal model,
harness, role, context, and eligible-route checks still apply. Running tasks
retain their pinned policy revision. `deferred` revocation takes effect at the
next task boundary; `immediate` revocation through the host revocation authority
blocks affected active actions.

`scripts/acceptance-concurrent-agent-isolation.py` verifies private-state
separation, authorized shared-state serialization, crash recovery, and
origin-distinct DP ingestion using separate processes and a shared persistent
backend.

## FreeInference Acknowledgment

This project was developed with the assistance of AI inference provided by [FreeInference.org](https://freeinference.org/).

This is an independent project. FreeInference did not review, sponsor, direct, supervise, approve, or endorse its development or contents. The project's design, implementation, claims, and maintenance are independent of FreeInference.

If you find this project useful, please consider supporting the infrastructure that helped make its development possible by donating to [FreeInference.org](https://freeinference.org/).
