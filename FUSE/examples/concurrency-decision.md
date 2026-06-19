# Concurrency Decision — "Check three independent files"

Calibrates: independent calls batched, dependent calls serialized. Concurrency principle fires on unsafe parallelization.

---

## Input

> Check whether `auth.py`, `db.py`, and `config.py` all import from the same `utils` module, then tell me which utility functions are shared.

---

## Strategy Pass

**Necessity:** Three files' import statements are external state not yet observed. Tool calls required.
Signals: none.

**Selection:** `grep` is the fitting tool — searching file contents for import patterns. `view` would read whole files; `grep` with an import pattern is scoped and faster.
Signals: none.

**Sequencing:** The three grep calls are independent — none depends on another's output. The follow-up question ("which utility functions are shared") depends on all three results. So: batch the three greps, then analyze.
Signals: none.

**Concurrency:** Three calls, no data dependency between them. They should be batched in a single message.
Signal check: If they were being serialized (one at a time), `false_serialization` (0.5) would fire. They're being batched correctly.
Signals: none.

**Resource Bounds:** Three scoped greps, each targeting one file with a specific pattern. Bounded.
Signals: none.

**Evidence Interpretation:** (post-results) Each grep result proves whether that file imports from `utils`. The combination proves which files share the dependency. The claim "which utility functions are shared" requires examining the specific import names — grep for `from utils import` will show the function names in each file.
Signals: none.

**Termination:** Not applicable — no failures.
Signals: none.

**Restraint:** Three calls is the minimum to check three files. No performative calls. No heavyweight-for-lightweight.
Signals: none.

---

## Gate

W_fuse = 0. Silent mode. Suppression condition applies: the calls are straightforward, tools are correct, no evidence ambiguity.

---

## Output

[Three grep calls issued in a single message, in parallel]

```
grep: "from utils import|import utils" in auth.py → from utils import hash_password, validate_token
grep: "from utils import|import utils" in db.py    → from utils import hash_password
grep: "from utils import|import utils" in config.py → (no matches)
```

`hash_password` is shared between `auth.py` and `db.py`. `validate_token` is used only in `auth.py`. `config.py` does not import from `utils`.

---

## What This Calibrates

Three independent reads with no data dependency → batched in one message. The follow-up analysis depends on all three results, so it's deferred until all return.

Anti-pattern: issuing the three greps one at a time, waiting for each to return before issuing the next. This triples the latency for no evidence gain — the calls are independent.

---

## Counter-Example: Unsafe Parallelization

If the task were "find where `hash_password` is defined, then check its callers":

```
Call A: grep for "def hash_password" (locate the definition)
Call B: grep for "hash_password(" (find callers)
```

These look parallelizable but **Call B's interpretation depends on Call A's result** — if `hash_password` is defined in `utils.py`, callers in `utils.py` itself need different scoping than callers elsewhere. Batching A and B would force B to guess at A's result.

**Signal:** `unsafe_parallelization` (1.0) would fire. The correct strategy: issue Call A first, then issue Call B with the knowledge of where the definition lives.

W_fuse = 1.0 < 1.5 → silent, but the calls are restructured before execution: A first, then B.
