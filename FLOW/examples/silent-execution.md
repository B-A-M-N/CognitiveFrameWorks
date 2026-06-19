# Silent Execution — "Fix typo in error message"

Calibrates: change touches no trigger areas, FLOW does not run.

---

## Input

> Fix the typo in the error message in `auth.py` — it says "credetials" instead of "credentials".

---

## Trigger Gate

Check each trigger area:

1. Retries / backoff / timeouts? — No. Editing a string literal.
2. Queues / streams / concurrency? — No.
3. Caches / memoization / invalidation? — No.
4. Startup / initialization? — No.
5. Hot paths / loops / scans? — No.
6. Build, test, CI, or dev workflow? — No.
7. Provider/API calls? — No.
8. Database or filesystem access? — No.
9. Complex abstractions? — No.
10. Long-lived maintenance burden? — No.

**No triggers present. FLOW does not run.**

---

## Output

[edit applied: "credetials" → "credentials"]

No FLOW evaluation. No surface lines. The change is purely mechanical and touches no operational surface.

---

## What This Calibrates

The trigger gate is FLOW's primary suppression mechanism. A typo fix in a string literal touches no operational surface — no retries, no queues, no caches, no hot paths, no external I/O. FLOW evaluating this change would be overhead with zero signal potential.

This is the distinction between FLOW and a general code review skill. A general review might comment on spelling, style, or naming. FLOW does not. FLOW only activates when the change touches an operational surface where drag could be introduced.

Anti-pattern to avoid: running FLOW on every change "to be thorough." The trigger gate exists precisely to prevent this. If no trigger fires, FLOW stays dormant.
