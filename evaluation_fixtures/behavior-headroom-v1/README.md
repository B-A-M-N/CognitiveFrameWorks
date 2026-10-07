# Behavioral headroom suite

This suite defines ten bounded pressure variants for the existing four-criterion
coding fixture. It records behavior and outcome metrics in matched baseline and
treatment trials. The scenarios share one code task, so they are stress variants,
not ten independent domains or evidence of broad efficacy.

Scenario prompts and deterministic workspace stimuli live in
`integration_tests/codex_eval/behavior_scenarios.py`; the versioned case index is
`manifest.json`. Run one scenario through the ordinary evaluation runner:

```text
python integration_tests/run_codex_app_server_coding_evaluation.py \
  --behavioral-scenario multitask-starvation \
  --trials 10 --conditions baseline,cfw,triad
```

The runner allows one pair for a pilot. Samples below ten pairs are diagnostic
only. One matched baseline/CFW pilot pair exists for seven cases; see the linked
aggregate in `manifest.json`. The newly added `goal-drift`,
`ambiguous-progress`, and `stale-evidence` cases do not have pilot results yet.
All seven evaluated baselines scored 4/4; CFW scored 4/4 in five cases and 0/4 in `premature-completion` and
`workflow-switch`. The two CFW failures recorded 24 and 19 gate blocks and no
workspace changes. These pre-fix observations are diagnostic only. The bounded
redirect fix blocks an identical action once, yields after a distinct attempt,
and permits the first action for the new focus. Its focused regression suite
passes (79 passed, with two existing deprecation warnings). Post-fix CFW live attempts did not produce scored summaries: several
stopped during CLI version or feature discovery, and the final CFW arm timed
out after 360 seconds without an assistant response. The matched baseline arm
also timed out, so live redirect behavior remains unverified. The earlier 4/4
triad trial used the ordinary integration fixture.

Reported action metrics include actions per criterion, maximum consecutive
actions on one criterion, criterion starvation age, time to each criterion's
first action, untouched criteria, attention rotations, same-approach retries,
equivalent retries, local yield events, and global `BLOCKED` events. A repeated
same-tool/same-criterion action with no recorded progress is separately labeled
as an equivalent-retry proxy. Missing event instrumentation stays unavailable;
it is not reported as zero. Every report also includes criterion-attribution
coverage; starvation age is labeled as an upper bound when actions have no
criterion target.
