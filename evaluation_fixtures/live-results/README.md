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
| [`coding-eval-20261006T051814Z.json`](coding-eval-20261006T051814Z.json) | 50 trials; 10 each for baseline, CFW, CFW+CSW, triad, and triad-multipath; `deepseek-v4-flash`. Baseline 10/10 at 4/4; CFW 7/10 at 4/4 (mean 2.8); each combined condition 10/10 at 4/4. | Dirty CFW/CSW/DP trees at `89e76d0`, `a80471f`, `f85df96`. | Integration and non-regression for one fixture/model. CFW-only's 3/10 incomplete runs require trace review; this is not causal evidence. |
| [`post-reuse-baseline-cfw-20261006.json`](post-reuse-baseline-cfw-20261006.json) | 20 trials across baseline and CFW; `deepseek-v4-flash`. Baseline 10/10 at 4/4; CFW 7/10 full score (mean 3.1). The three incomplete CFW runs used all three turns (scores 0/4, 3/4, 0/4), with six `convergence.total_action_limit` reason observations and no goal reanchors. Validator evidence reuse was observed 40/40 times. | Dirty trees; report records working-tree and untracked-manifest hashes. | Goal-reanchor fix and validator-reuse development check; noncanonical. The report lacks action-level traces for those failures, so their within-turn cause remains unresolved. |
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

The seven scenario variants are implemented and indexed in
[`behavior-headroom-v1`](../behavior-headroom-v1/README.md). They apply bounded
pressure variants to the same four-criterion coding fixture; they are not seven
independent domains. One matched baseline/CFW pilot pair now exists for each
scenario. The aggregate is
[`noncanonical-behavior-headroom-suite-pilot-20261006.json`](noncanonical-behavior-headroom-suite-pilot-20261006.json).
All seven baselines scored 4/4. CFW scored 4/4 in five scenarios and 0/4 in
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
applicability, gate receipts, normalized DP intervention accounting, and
bounded behavioral traces. For a pressure case, add
`--behavioral-scenario <scenario-id>`; the runner permits one-pair pilots and
requires ten pairs before treating a sample as a benchmark. Trace comparisons
are descriptive associations, not causal estimates. Internal Codex inference
and planning boundaries remain outside host interception.
