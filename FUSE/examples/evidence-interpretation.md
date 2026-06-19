# Evidence Interpretation — "The test passes, so the feature works"

Calibrates: green test overclaimed as proof the feature works. Evidence Interpretation fires, claim is narrowed.

---

## Input

> I ran the test suite for the login function and it passed. The login feature is working now.

---

## Context

The test suite contains one test: `test_login_with_valid_credentials`. It tests that a valid username/password combination returns a 200 status. It does not test invalid credentials, locked accounts, expired sessions, rate limiting, or concurrent logins.

---

## Strategy Pass (post-execution)

**Necessity:** The test has been run. The result is observed (pass). The claim being made — "the login feature is working" — is broader than what the test result proves.
Signals: none (necessity is about whether to call a tool; the tool was already called correctly).

**Selection:** Not applicable — tool already selected and run.
Signals: none.

**Sequencing:** Not applicable — post-execution evaluation.
Signals: none.

**Concurrency:** Not applicable.
Signals: none.

**Resource Bounds:** Not applicable.
Signals: none.

**Evidence Interpretation:** The test result (pass) proves: the valid-credentials path returns 200. The claim being made ("login feature is working") asserts: the feature works across its operating range. The test covers one path out of many (valid credentials only). The evidence does not support the claim's scope.
Signal: `overclaimed_evidence` (1.0) — the test result is being interpreted more broadly than it warrants.

**Termination:** Not applicable.
Signals: none.

**Restraint:** Not applicable.
Signals: none.

---

## Gate

W_fuse = 1.0 < 1.5 → silent.

The signal does not surface to the user, but it does propagate downstream:

- **ANCHOR Epistemic Classification:** the claim "login feature is working" is reclassified from Verified to Inferred. The evidence supports "valid-credentials path works," not "feature works."
- **SISPIS:** +1 to `tradeoff_density`. The output must frame the claim at its evidenced scope, not the claimed scope.

---

## Output

The login test passes for valid credentials. The tested path (valid username/password → 200) is verified. Other paths — invalid credentials, locked accounts, rate limiting, concurrent logins — are not covered by this test and remain unverified.

---

## What This Calibrates

Evidence Interpretation runs after tool results arrive, not just before tool calls. A correct tool call (running the test) can still produce an incorrect claim (overreading the result). FUSE catches the mismatch between what the result proves and what is being asserted.

The claim is narrowed to match the evidence. "The feature works" becomes "the tested path works." The untested paths are labeled unverified, not assumed working.

Anti-pattern to avoid: "Great, the login feature is working now!" — this asserts the broader claim the test doesn't prove. The test proves what it tests. State that scope.

---

## ANCHOR Integration

The reclassification flows to ANCHOR's Epistemic Classification:

| Claim | Prior class | Post-FUSE class | Reason |
|-------|-------------|-----------------|--------|
| Valid-credentials path returns 200 | Verified | Verified | Test proves this directly |
| Login feature is working | Inferred | Inferred (confirmed) | Test is consistent with this but doesn't prove it |
| Invalid credentials are handled | Unknown | Unknown | Not tested, not inferred |

No state transition needed — the classification was already correct internally. FUSE's signal confirms it and ensures the output reflects it.
