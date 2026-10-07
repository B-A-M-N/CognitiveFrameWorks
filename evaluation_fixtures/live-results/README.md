# Direct Codex live results

These artifacts use regular packaged Codex through the generic App Server
protocol. HarvardCodex Control is a separate product and was not used for the
trials indexed here. The model sees only host-registered CFW tools; DP's
control MCP stays private to the trusted host.

## What this fixture measures

The four-defect coding fixture is an integration and non-regression check. The
baseline has repeatedly scored 4/4, leaving no headroom to show behavioral
improvement. A full score here establishes that the stack can complete this
task under the recorded setup; it does not establish comparative efficacy or
general coding-agent performance.

## Canonical results

None yet. A result becomes canonical only after each repository has a clean
working tree, exact CFW/CSW/DP commit SHAs, and source-manifest hashes recorded
in the report. All existing multi-repository matrices below were run against
dirty trees.

## Noncanonical integration and score runs

| Artifact | Setup and result | Tree state | Claim |
| --- | --- | --- | --- |
| [`codex-app-server-workspace-routing-preflight-20261007T065902Z.json`](codex-app-server-workspace-routing-preflight-20261007T065902Z.json) | User-approved ChatGPT-mode preflight with `gpt-6-luna`; App Server classified workspace routing unavailable. No host, triad, thread, or turn started. | Dirty development trees; preflight-only result records no repository mutation. | Infrastructure diagnostic only. It has no model or efficacy observation. |
| [`behavior-headroom-equivalent-retry-20261007T061225Z.json`](behavior-headroom-equivalent-retry-20261007T061225Z.json) | One matched baseline/CFW pair scheduled after the adapter began forwarding explicit write-change observations. Both arms stopped at ChatGPT App Server workspace-routing preflight; zero host starts and zero model calls. | Dirty development trees; report records state fingerprints. | Infrastructure-only attempt. It does not evaluate the adapter change or model behavior. |
| [`behavior-headroom-equivalent-retry-20261007T060625Z.json`](behavior-headroom-equivalent-retry-20261007T060625Z.json) | One matched baseline/CFW pair; both scored 4/4 externally. CFW made 50 model tool calls and 14 validator calls; its completion gate blocked 10 calls after the write result's `changed` observation was omitted at the adapter boundary. | Dirty development trees; report records state fingerprints. | Diagnostic trace that exposed missing artifact-change evidence. The CFW completion denial is not a task-score failure. |
| [`behavior-headroom-equivalent-retry-20261007T055248Z.json`](behavior-headroom-equivalent-retry-20261007T055248Z.json) | Ten matched baseline/CFW pairs; both conditions scored 10/10 at 4/4. The prompt did not produce any same-approach or equivalent retries. | Dirty development trees; report records state fingerprints. | Stimulus check only; it did not exercise retry behavior and is not retry-efficacy evidence. |
| [`coding-eval-20261007T053122Z.json`](coding-eval-20261007T053122Z.json) | 10 matched baseline/CFW pairs; ChatGPT auth with `gpt-6-luna`; three-turn budget. Both conditions 10/10 valid at 4/4. Each CFW trial records 14–35 action entries, 28–70 gate receipts, and one completion attempt. | Dirty CFW/CSW/DP trees; exact HEADs, working-tree hashes, untracked manifests, and source fingerprints are in the report. | Trace-instrumented CFW reliability rerun. It does not reproduce the historical 7/10 result because the model/auth cohort differs; all 10 CFW trials passed, so this sample has no failure cohort to diagnose. |
| [`coding-eval-20261006T051814Z.json`](coding-eval-20261006T051814Z.json) | 50 trials; 10 each for baseline, CFW, CFW+CSW, triad, and triad-multipath; `deepseek-v4-flash`. Baseline 10/10 at 4/4; CFW 7/10 at 4/4 (mean 2.8); each combined condition 10/10 at 4/4. | Dirty CFW/CSW/DP trees at `89e76d0`, `a80471f`, `f85df96`. | The three CFW-only misses scored 0/4 and ended with `require_goal_reanchor`; item counters show respectively 16 reads/7 validator calls/6 writes, 55/32/1, and 18/5/4. The normalized artifact records zero gate receipts while a legacy invariant says `cfw_action_gate_observed=true`, so gate execution is not substantiated by receipts. Per-call arguments, results, and action traces were not retained; repeated reads/validation are only a hypothesis, not a causal finding. A same-cohort trace-instrumented rerun is still needed. |
| [`post-reuse-baseline-cfw-20261006.json`](post-reuse-baseline-cfw-20261006.json) | 20 trials across baseline and CFW; `deepseek-v4-flash`. Baseline 10/10 at 4/4; CFW 7/10 full score (mean 3.1). The three incomplete CFW runs used all three turns (scores 0/4, 3/4, 0/4), with six `convergence.total_action_limit` reason observations and no goal reanchors. Validator evidence reuse was observed 40/40 times. | Dirty trees; report records working-tree and untracked-manifest hashes. | Noncanonical reliability check. Its action-level cause remains unresolved. Current source routes total-action exhaustion through a bounded changed-hypothesis redirect for direct actions while preserving verification and concrete blocker-removal actions. A later trace-instrumented run completed 10/10 with eight CFW gate blocks, but used a different model/auth cohort and cannot establish that this change caused the improvement. |
| [`coding-eval-20261006T044239Z.json`](coding-eval-20261006T044239Z.json) | 50 trials across the same five conditions; `deepseek-v4-flash`. CFW reached 9/10 at 4/4; the other four conditions reached 10/10. | Dirty CFW/CSW/DP trees. | Earlier integration and non-regression sample. |
| [`coding-eval-20261005T204440Z.json`](coding-eval-20261005T204440Z.json) | 40 trials across baseline, CFW, CFW+CSW, and triad; `gpt-6-luna`. Baseline 10/10, CFW 6/10, combined conditions 10/10 at 4/4. | Dirty development trees. | Historical score and completion-policy evidence; not a clean comparison. |
| [`noncanonical-triad-20261006.json`](noncanonical-triad-20261006.json) | One triad run; `deepseek-v4-flash`; 4/4 criteria, 24 model tool calls, 5 validator calls, 48 CFW gate receipts, and 65 committed DP control ticks. | Dirty development tree. Codex CLI `0.160.0`; full catalog SHA-256 `3310a31f8ec0d7a985fb07cf6043bc060dc5c438c4b23058e14b9b1144b86714`. | One successful integration sample only. Not an efficacy estimate or canonical result. |
| [`noncanonical-behavior-headroom-multitask-pair-20261006.json`](noncanonical-behavior-headroom-multitask-pair-20261006.json) | One matched baseline/CFW pair on `multitask-starvation`; `deepseek-v4-flash`; both 4/4. Baseline: 15 actions, 93.3% criterion attribution, 10 rotations. CFW: 15 actions, 73.3% attribution, 7 rotations. Both touched all four criteria; local-yield and global-blocked instrumentation was unavailable. | Dirty development tree; one pair only. | Pilot confirms the scenario and metrics execute. It does not estimate efficacy or support a causal comparison. |

The one-trial endpoint probe first tried `gpt-6-luna`; FreeInference returned
`model not found` before any host tool call. The successful sample used the
model ID already present in the earlier live matrices, `deepseek-v4-flash`.

## Noncanonical auth diagnostics

[`noncanonical-aegiskeys-freeinference-auth-20261006.json`](noncanonical-aegiskeys-freeinference-auth-20261006.json)
records a regular-Codex triad attempt whose credential was injected from
AegisKeys through its child-only `with` command. FreeInference returned `401`
for the stored OpenAI-compatible key before assistant output or host-tool
calls. The attempt is unscored and does not count toward any trial result. The
other FreeInference profiles in AegisKeys target its Anthropic route and were
not substituted for the Codex Responses endpoint.

The later [`noncanonical-aegiskeys-freeinference-triad-final-20261006.json`](noncanonical-aegiskeys-freeinference-triad-final-20261006.json)
attempt confirms that CFW, shared CSW authority, and the private DP bridge
initialized successfully, with zero model-visible DP tools and two committed
DP control ticks. The first Responses request still returned `401`; no
assistant output, model tool call, or behavioral score was produced. Further
provider calls are paused until the OpenAI-compatible AegisKeys entry is
updated.

## Noncanonical host-boundary diagnostics

The `gate-eval-*.json` files exercise whether the host rejects one protected
write across baseline and treatment conditions. The
`generic-codex-multipath-boundary-*.json` files are no-model startup and
boundary probes, including direct packaged CLI and native-feature closure
checks. These prove host setup only; they do not measure model effectiveness.

The remaining `generic-codex-multipath-*.json`, `post-reuse-cfw-smoke-*.json`,
and `postfix-baseline-cfw-pre-reuse-partial-20261006.json` files preserve
historical model-start failures, routing diagnostics, and intermediate runs.
They are retained for debugging and are not canonical. ChatGPT-mode attempts
that failed during workspace-routing discovery are infrastructure failures;
they do not count as model behavior or treatment outcomes.

## Behavioral headroom benchmark

Ten scenario variants are defined and indexed in
[`behavior-headroom-v1`](../behavior-headroom-v1/README.md). They apply bounded
pressure variants to the same four-criterion coding fixture; they are not ten
independent domains. One matched baseline/CFW pilot pair exists for seven
scenarios. `goal-drift`, `ambiguous-progress`, and `stale-evidence` are newly
defined and have no pilot results yet. The existing aggregate is
[`noncanonical-behavior-headroom-suite-pilot-20261006.json`](noncanonical-behavior-headroom-suite-pilot-20261006.json).
All seven evaluated baselines scored 4/4. CFW scored 4/4 in five scenarios and 0/4 in
`premature-completion` and `workflow-switch`; those two runs made no workspace
changes and recorded 24 and 19 CFW gate blocks. These are pre-fix diagnostic
outcomes from one pair per scenario, not efficacy or causal evidence.

The repeated progress-redirect gate was then bounded: an identical action is
blocked without restating the redirect, and a second distinct attempt yields
attention to another runnable criterion and proceeds. The first action on the
new focus is allowed once. The focused regression suite passes (79 passed, with
two existing deprecation warnings). Post-fix CFW live
attempts did not produce scored summaries: several stopped during CLI version
or feature discovery, and the final CFW arm timed out after 360 seconds without
an assistant response. The matched baseline arm also timed out. The live
behavior change remains unverified; these diagnostics are included in the
aggregate and do not count as scenario scores.

Action-level metrics include actions per criterion, attribution coverage,
maximum consecutive actions on one criterion, starvation-age upper bounds,
time to first attributed action, untouched criteria, attention rotations,
same-approach retries, equivalent retries and their separately labeled proxy,
local yields, and global `BLOCKED` events. Local-yield and global-blocked event
counts are unavailable in these pilot traces and remain null. Unknown
attribution is retained and reported; it is not silently treated as evidence
of attention.

## Reproduction

Use the explicitly selected regular Codex binary and provider. Credential
precedence is `--provider-env-file`, `CODEX_PROVIDER_ENV_FILE`, then the named
environment variable. Do not put credential values in command arguments or
reports.

```bash
python integration_tests/run_codex_app_server_coding_evaluation.py \
  --trials 10 --max-turns 3 --model deepseek-v4-flash \
  --conditions baseline,cfw,cfw-csw,triad,triad-multipath \
  --auth-mode external-provider --provider-id freeinference \
  --provider-base-url https://freeinference.org/v1 \
  --provider-env-key FREEINFERENCE_API_KEY --provider-wire-api responses
```

The evaluator records score distributions, turn/tool/write counts, completion
applicability and observed completion decisions, gate receipts, normalized DP
intervention accounting, and bounded behavioral traces. Baseline completion is
`not_applicable`; a treatment trial that never reaches completion remains
applicable but unobserved. Reports also retain bounded action-call metadata
(tool, call ID, success, and validator ID; no arguments or results), count
model-initiated validator calls separately from host validator executions, and
group host validator runs and timing by execution channel. Completion attempts
include their validator-to-criterion mappings and pass/reuse status. For a
pressure case, add
`--behavioral-scenario <scenario-id>`; the runner permits one-pair pilots and
requires ten pairs before treating a sample as a benchmark. Trace comparisons
are descriptive associations, not causal estimates. Internal Codex inference
and planning boundaries remain outside host interception.
