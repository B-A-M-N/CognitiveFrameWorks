# Enforcement: instructions versus structural authority

One of CFW's important technical characteristics is that **not every component
is an absolute control mechanism**. Some components guide model behavior; others
interact with trusted-host policy and action gates.

A reader should be able to tell, for any given safeguard, which of these it is.

## Three enforcement classes

| Class | What it means | Who guarantees it |
| --- | --- | --- |
| **Structural enforcement** | The action cannot execute, regardless of what the model intends | The trusted host, through CFW's action gate |
| **Model adherence** | The model is instructed, and compliance depends on the model | The model |
| **Capable-host integration** | The boundary exists only when the host wires it | The host integration |

### What is structurally enforced

CFW's action gate is the primary structural boundary. A trusted host evaluates
each proposed action through CFW's structural policy before execution; a
rejected action does not run, whatever the model prefers.

Component floors are also structural in combined mode: CFW rejects any
DigitalPsychology projection that would drop behavior below a selected
component's baseline. Transient operations such as `reset`, `reopen`, and
`recovery` are validated separately from baseline modes. Malformed or ineligible
advice is rejected as a whole.

### What relies on model adherence

The components themselves — reasoning discipline, evidence interpretation,
communication calibration — are largely advisory *from the runtime's point of
view*. They enter model context and shape behavior, but a model that ignores them
is not blocked by a gate. That is precisely why the structural gate exists.

### What depends on a capable host

Several important boundaries require host integration to have any effect:

- Pre-action blocking and completion interception, which require the host to call
  the explicit boundaries.
- WARD enforcement, which constrains destructive actions and secrets through the
  host's action policy.
- Attention leasing and projection of the active goal window.
- Any harness-specific capability, which requires a verified adapter identity.

Without the host calling those boundaries, unverified identities expose no
effective capabilities and produce explicit degradation claims rather than
silent gaps.

## The invariant

> **Behavioral learning may refine legal choices. It must not make an illegal
> choice legal.**

CFW pins its own component and enforcement policy separately from the host's
composite runtime identity. The CFW policy hash covers CFW-owned contracts; the
composite snapshot also covers the CSW registry and workflow plan, the host goal
contract, and the model, harness, and toolset. A workflow or toolset change
therefore changes the composite identity without changing the CFW policy hash
when CFW's own policy is unchanged.

## What CFW does not overflow

- CFW cannot bypass its own preflight or final action gate.
- DP evidence can prefer or suppress an *eligible* route; it cannot bypass
  authority, required evidence, mandatory StateWork transitions, or structural
  safety policy.
- CFW does not carry semantic-provider policy; that is configured in DP.
