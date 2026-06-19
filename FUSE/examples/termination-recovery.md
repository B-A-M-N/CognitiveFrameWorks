# Termination Recovery — "The same edit fails three times"

Calibrates: retry bound exceeded, FUSE escalates to ANCHOR Recovery Discipline. One merged block, not two.

---

## Input

> Fix the failing test in `auth_test.py`. The test expects `login()` to return a token, but it's returning None.

---

## Execution History

1. **Attempt 1:** Read `auth.py`. Found `login()` returns `None` on line 42 when the password hash doesn't match. Edited the hash comparison. Ran test. Failed — same error.
2. **Attempt 2:** Read the test. Found it uses a plaintext password. Edited `login()` to hash the input before comparison. Ran test. Failed — same error.
3. **Attempt 3 (about to start):** About to try editing the hash function itself.

---

## Strategy Pass (before attempt 3)

**Necessity:** The test has been run twice and failed both times. The code has been read. A third edit is being proposed. The question is whether the approach is wrong, not whether more tool calls are needed.
Signals: none.

**Selection:** The tools (view, edit, bash for test) are correct for the task. No mismatch.
Signals: none.

**Sequencing:** Read-before-edit has been followed. Verify-before-claim has been followed (test was run). Sequencing is correct.
Signals: none.

**Concurrency:** Single-threaded debugging. No concurrency issue.
Signals: none.

**Resource Bounds:** Scoped reads, scoped edits. No unbounded operations.
Signals: none.

**Evidence Interpretation:** The test result (fail, same error) proves: the fix didn't change the tested behavior. This is being interpreted correctly — the approach isn't working.
Signals: none.

**Termination:** This is the third attempt at the same fix. The first two retries used varied inputs (different edits), but the underlying approach — fixing the hash comparison — has not worked. The same error recurring means the approach is wrong, not the execution. A third retry of the same approach would be `retry_without_variation` at the approach level.
Signal: `retry_without_variation` (1.0) — the same approach is being retried without fundamental variation.

After classifying: retry bound (2) has been exceeded. The approach has failed twice.
Signal: `retry_bound_exceeded` (1.0) — escalation to ANCHOR Recovery Discipline required.

**Restraint:** Not applicable.
Signals: none.

---

## Gate

W_fuse = 2.0 (1.0 + 1.0) >= 1.5 → Surface mode.

Both signals surface. `retry_bound_exceeded` triggers ANCHOR Recovery Discipline handoff.

---

## FUSE-ANCHOR Merged Block

```
**Termination:** The hash-comparison fix has been attempted twice with variations and the test fails identically both times. Continuing the same approach will not resolve it.
**[Recovery Discipline]:** Last verified: test fails, login() returns None. New approach: the issue may not be in the hash comparison — check whether login() is being called with the arguments the test expects, or whether an earlier return path is intercepting. Reset cost: one additional read of the full login() control flow.

[action: view auth.py login() full function body to trace control flow]
```

---

## What This Calibrates

Termination fires when retries accumulate, not when a single call fails. The distinction: retrying once with variation is correct debugging. Retrying the same approach a third time is sunk-cost behavior.

FUSE owns the finding (what failed, how many times) and the implication (continuing costs more cycles without progress). ANCHOR owns the recovery procedure (last verified state, new approach, cost). These merge into one block — the same contract OWL and ANCHOR use for `approach_failed`.

The new approach is not "try harder." It is "re-examine the problem." The fix attempts focused on the hash comparison; the recovery redirects to the control flow — a different hypothesis about where the bug lives.

Anti-pattern to avoid: attempting the same fix a third time with minor syntactic variation. If two genuine variations failed, the approach is wrong. Escalate, don't persist.

---

## ANCHOR Integration

| State | Value |
|-------|-------|
| Approach health | Active → Degraded → Failed → Recovered |
| Last verified state | Test fails; login() returns None |
| Rejected approach | Fixing hash comparison (attempted 2x, no change) |
| New approach | Trace full control flow of login() |
| Recovery cost | 1 additional read |

The Recovery Discipline transition is: Failed → Recovered (new approach proposed). If the new approach also fails, the cycle repeats — but ANCHOR tracks that this is now the second rejected approach, raising the stakes for the third.
