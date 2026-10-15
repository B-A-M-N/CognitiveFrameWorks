# Qualification

Real-agent benchmarks, cohort experiments, and release qualification for
CognitiveFrameWorks.

## The headline caveat

> **CFW provides testable behavioral policies and structural enforcement.
> Passing structural tests does not, by itself, demonstrate improvements in model
> reasoning or task success.**

Everything below elaborates that distinction. Structural tests establish that the
*mechanism* works. Only real-agent qualification can speak to whether an agent
behaves better, and even then the recorded results have limits stated explicitly.

## Core validation surfaces

```bash
python3 scripts/validate-framework.py
python3 scripts/test-runtime.py
python3 scripts/acceptance-public-beta.py
python3 scripts/acceptance-session-routing.py
python3 scripts/acceptance-clean-install.py
python3 scripts/acceptance-upgrade-install.py
python3 scripts/doctor.py --registry my-host="$HOME/.my-host/skills"
```

Different tests establish different claims. A deterministic structural acceptance
test can prove that a host gate blocks an illegal action. It cannot prove that a
real model reasons differently. Behavioral claims therefore require real-agent
qualification in addition to deterministic runtime tests.

## What has been demonstrated

The Codex fixture results establish **integration and non-regression** properties:

- The four-defect coding fixture: the recorded baseline reached 4/4 in all ten
  runs, so the fixture has **no baseline headroom** for a behavioral-superiority
  claim.
- The 50-trial dirty-tree matrix recorded CFW at 7/10 full scores and CFW+CSW,
  triad, and triad-multipath at 10/10 each.

These results establish integration and sample-specific non-regression only;
they do not establish general efficacy. The
[live-results index](../evaluation_fixtures/live-results/README.md) records the
exact artifacts and limitations.

### Matched coding evaluation

The matched coding evaluation records a canonical runtime cohort identity on each
trial, including model/provider pins, task and binding digests, source snapshots
for CFW/CSW/DP, the Codex feature catalog, and Laya configuration. Paired
efficacy deltas are counted only when both trials have valid, identical cohort
hashes. Missing, malformed, or different identities remain visible as excluded pair
statuses and cannot support a same-cohort causal claim.

Trial summaries classify outcomes as `PASS`, `FAIL`, `INVALID_TREATMENT`,
`INFRASTRUCTURE_BLOCKED`, `CAPABILITY_BLOCKED`, or `INSUFFICIENT_EVIDENCE`;
matched-pair reporting adds `INCOMPARABLE_COHORT` when identity hashes differ.

### Completion validators

Completion validators use `CodexValidatorBinding` entries keyed to host-owned
acceptance criteria. Each entry fixes the action binding and arguments, declares
the criteria and boundary evidence it covers, and validates output against a
host-authored JSON Schema. `validate_completion()` aggregates validator evidence
with typed completion receipts from CFW and the host-selected runtime authorities.
Combined mode requires both DP and CSW receipts; the runtime then rechecks its
completion permit and current workflow state before finalizing. Missing, blocked,
or failed validators or authorities leave the task incomplete; the model cannot
supply a pass boolean or invent evidence.

### Adapter contract fixture

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
only whether each host boundary blocks this fixture's protected write; it is not a
general coding-agent effectiveness claim.

## Release qualification

Structural public-beta checks, hostile integration, real-agent behavioral
qualification, and operational performance are separate evidence classes. See
`contracts/qualifications/`; unexecuted results remain `not_run`.

`scripts/acceptance-real-agent-benchmark.py` measures task outcomes only. Its
report is explicitly ineligible as intervention evidence because it does not
assign system cohorts or verify host-applied controls. Real-agent qualification
must include host receipts and independent outcome attribution before it can pass.

### Release gate

The authoritative automated release gate is
[`release-gates.json`](../release-gates.json). It covers CFW
runtime/public API/structural/session routing, CSW
validation/control-plane/concurrency, DP self-tests/routing/cross-system,
guard-policy causal acceptance, the private-MCP runtime triad, dynamic
improvement, package resources, installation, upgrade, and hostile-install checks.
The runtime-triad gate runs the current DP, CFW, and CSW authorities together and
qualifies restart reconciliation, CFW final-gate rejection, lease ownership, and
twelve-criterion attention coverage.

The public-beta gate uses the three local checkout layout. Standalone CFW behavior
is separately exercised by the isolated install, doctor, and quickstart commands
in the README.

Structural tests establish deterministic policy, authority, state, and integration
behavior. They do not establish that an external model reasons differently.
Real-agent claims require
[`scripts/acceptance-real-agent.py`](../scripts/acceptance-real-agent.py) with an
explicit external agent command and complete objective qualification. Host
integrations, model availability, tool permissions, and evaluator behavior remain
deployment-specific limitations.

The final release gate requires every qualification marked
`mandatory_for_release` to have a `pass` result; `not_run`, `fail`, and `blocked`
results stop release.

## Cohort experiments

The production cohort runner retains durable evidence only when given an explicit
`--evidence-dir`. Either Laya arm requires this option. The default matrix
includes the deterministic DP and Laya shadow arms; it never enables advisory mode
implicitly:

```sh
python scripts/cohort/run_cohort.py \
  --configurations cfw_csw_dp,cfw_csw_dp_laya \
  --evidence-dir /path/to/private/cohort-evidence
```

The `cfw_csw_dp_laya_advisory` arm is explicit opt-in. Before running any
canaries, the runner validates the supplied DP calibration bundle and requires at
least one active, qualified, drift-cleared record for an enabled question family,
plus a bounded experiment ID:

```sh
DIGITALPSYCHOLOGY_SYSTEM_ONE_CALIBRATION_PATH=/path/to/qualified-bundle.json \
DIGITALPSYCHOLOGY_SYSTEM_ONE_ACTUATION_EXPERIMENT_ID=bounded-study-1 \
python scripts/cohort/run_cohort.py \
  --configurations cfw_csw_dp,cfw_csw_dp_laya,cfw_csw_dp_laya_advisory \
  --evidence-dir /path/to/private/cohort-evidence
```

The shadow arm explicitly forces `DIGITALPSYCHOLOGY_SYSTEM_ONE_MODE=shadow`, even
if the invoking shell has advisory mode configured. The advisory arm forces
`advisory`; DP still requires the calibration to match the loaded model, protocol,
and query context before any selection can change.

Before starting the runner, set `DIGITALPSYCHOLOGY_ROOT` to the DP checkout being
evaluated when it differs from the sibling `DigitalPsychology` path. The manifest
records the actual DP revision used by the host process.

Each run gets a unique private directory containing a manifest, per-cell benchmark
reports and trial JSONL, the host's DP event and behavior databases, the task
workspaces, and the preflight report. The manifest captures the CFW, CSW, and DP
Git revisions plus a digest of tracked local changes. Trial records contain host
evaluator results, runtime behavior telemetry, treatment exposure, and action
receipts; they omit the agent conversation trajectory. The task workspaces can
contain fixture and source contents, so use a user-owned directory that is not
writable by other users. Run directories are mode `0700`, and JSON evidence files
are mode `0600`. The manifest remains `running` if the process stops before
completion, while already fsynced trial records and runtime state remain available
for review.

When matched configurations are run together, the report pairs trials by their
stable `task_id` and reports treatment-minus-control outcome and evaluator-metric
deltas separately for assigned and holdout trials. These are descriptive paired
results with no confidence interval; **they do not qualify Laya by themselves.**
Paired rows also include model-call, token, host-action, and semantic-use counts.
The cohort summary uses the final cumulative DP record once per task and reports
query opportunities, attempts, results, planner skips by reason, selection flips,
and measured query latency. Observation-value estimates are kept labeled as
predictions; they are not measured utility.

## FreeInference Acknowledgment

This project was developed with the assistance of AI inference provided by [FreeInference.org](https://freeinference.org/).

This is an independent project. FreeInference did not review, sponsor, direct, supervise, approve, or endorse its development or contents. The project's design, implementation, claims, and maintenance are independent of FreeInference.

If you find this project useful, please consider supporting the infrastructure that helped make its development possible by donating to [FreeInference.org](https://freeinference.org/).
