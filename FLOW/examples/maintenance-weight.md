# Maintenance Weight — "Abstraction that adds coupling without reducing complexity"

Calibrates: abstraction layer that doesn't simplify the call site but introduces tight coupling. `coupling_burden` and `unnecessary_abstraction` both fire.

---

## Input

> Refactor the user lookup to use a repository pattern for flexibility.

```python
# Before — direct query
def get_user(user_id):
    return db.query("SELECT * FROM users WHERE id = ?", user_id)

# After — repository pattern
class UserRepository:
    def __init__(self, db):
        self.db = db

    def find_by_id(self, user_id):
        return self._execute_query("SELECT * FROM users WHERE id = ?", user_id)

    def _execute_query(self, query, *params):
        return self.db.query(query, *params)

# Usage
repo = UserRepository(db)
user = repo.find_by_id(user_id)
```

---

## Trigger Gate

1. Retries / backoff / timeouts? — No.
2. Queues / streams / concurrency? — No.
3. Caches / memoization / invalidation? — No.
4. Startup / initialization? — No.
5. Hot paths / loops / scans? — No.
6. Build, test, CI, or dev workflow? — No.
7. Provider/API calls? — No.
8. Database or filesystem access? — No (the DB access pattern hasn't changed, just wrapped).
9. Complex abstractions? — **Yes.** A new abstraction layer (repository pattern) is being introduced.
10. Long-lived maintenance burden? — **Yes.** The pattern introduces coupling that will affect future changes.

**Triggers present: complex abstractions, long-lived maintenance burden. FLOW runs. Principle to evaluate: Maintenance Weight.**

---

## Efficiency Pass

### Maintenance Weight

The repository pattern is being introduced for "flexibility." Evaluate against the principle: does the abstraction reduce more complexity than it adds?

**Call site comparison:**
- Before: `user = db.query("SELECT * FROM users WHERE id = ?", user_id)` — one line, direct.
- After: `repo = UserRepository(db); user = repo.find_by_id(user_id)` — two lines, indirect.

The call site is not simpler. It requires instantiating the repository, passing the db, and calling through an extra layer. The `_execute_query` method is indirection without benefit — it wraps `db.query` with no additional logic.

**Coupling analysis:**
- Before: `get_user` depends on `db` (one dependency).
- After: `get_user` depends on `UserRepository` which depends on `db` (two dependencies, one new).

The abstraction adds a dependency layer without reducing any. Future changes to the query now require editing the repository class, not just the function. The repository class becomes a coupling point — every caller depends on its interface.

**Second use case check:**
Is there a concrete second use case for the repository? The request says "for flexibility" — this is hypothetical, not concrete. There is no second implementation, no test double that requires the interface, no swap-out scenario that's been discussed.

**Signals:**
- `unnecessary_abstraction` (0.5) — the abstraction is being added without a concrete second use case; the call site is not simpler with the abstraction than without
- `coupling_burden` (1.0) — the change introduces coupling (every caller now depends on the repository interface) that will complicate future modifications without reducing complexity at the call site

Both signals fire. They are from the same principle but represent distinct findings: `unnecessary_abstraction` is about the lack of justification; `coupling_burden` is about the ongoing cost. Per stacking rules, both can surface — they don't suppress each other because they describe different aspects of the same change.

---

## Gate

W_flow = 1.5 (1.0 + 0.5) >= 1.5 → Surface mode.

---

## SISPIS Integration

- `coupling_burden` → `downstream_impact` +1, `tradeoff_density` +1
- `unnecessary_abstraction` → `tradeoff_density` +0.5

Total: `downstream_impact` +1, `tradeoff_density` +1.5 (capped at 2.0).

E elevates moderately. SISPIS gate depends on base E, but the tradeoff density elevation means the output should frame this as a design decision, not just deliver the refactored code.

---

## Output

```
**Maintenance Weight:** The repository pattern adds a coupling layer (every caller now depends on UserRepository) without simplifying the call site. The _execute_query method is indirection without logic.
**Maintenance Weight:** The abstraction is introduced "for flexibility" but there is no concrete second use case — no second implementation, no test double requiring the interface, no swap-out scenario.

The direct query is simpler at the call site, has fewer dependencies, and is easier to modify. If a second use case emerges (e.g., a test double, a cached implementation, a different data source), the repository pattern is the right abstraction. Introducing it before that use case exists adds maintenance cost with no benefit.
```

---

## What This Calibrates

This is the hardest FLOW judgment to make, because abstractions are genuinely useful — sometimes. The distinction FLOW draws:

- **Justified abstraction:** There's a concrete second use case. The call site is simpler with the abstraction. The abstraction reduces more complexity than it adds.
- **Unjustified abstraction:** There's only a hypothetical use case ("for flexibility"). The call site is not simpler. The abstraction adds a layer that must be maintained.

The repository pattern is a good pattern — when there's a reason for it. A test suite that needs mock data sources. A migration from SQL to NoSQL. Multiple data backends. Without one of those, it's a class that wraps one method and calls it `_execute_query`.

The test FLOW applies: "Will this code be easier or harder to change in six months?" With the repository, changing the query means editing the repository class. Without it, changing the query means editing the function. The repository adds a step without removing one.

Anti-pattern to avoid: "We should use a repository pattern for flexibility." Flexibility for what? If the answer is "in case we need it," that's hypothetical flexibility — maintenance cost with no benefit. Add the abstraction when the second use case arrives, not before.

---

## OWL Integration

OWL's Simplicity principle would also fire here: "Minimum code that solves the problem." The repository pattern adds a class, a method, and a layer that the direct query doesn't need. OWL's Generalization principle would fire too: "No abstractions for single-use code." There is one use case (user lookup by ID), and the abstraction is for that single use case.

FLOW and OWL overlap on this case but from different angles:
- OWL's Simplicity: the code is more complex than the problem requires (synchronous judgment).
- OWL's Generalization: an abstraction is being added without justification (synchronous judgment).
- FLOW's Maintenance Weight: the abstraction will create ongoing maintenance burden (longitudinal judgment).

All three fire. The OWL signals surface first (OWL runs before FLOW in the pipeline). FLOW's signal adds the longitudinal dimension — it's not just "too complex now," it's "will be harder to maintain over time."

---

## When This Abstraction Would Be Justified

To calibrate the boundary, here's when the same repository pattern would NOT trigger FLOW:

```python
class UserRepository:
    def __init__(self, db):
        self.db = db

    def find_by_id(self, user_id):
        return self.db.query("SELECT * FROM users WHERE id = ?", user_id)

    def find_by_email(self, email):
        return self.db.query("SELECT * FROM users WHERE email = ?", email)

    def find_active_users(self):
        return self.db.query("SELECT * FROM users WHERE active = 1")

# Test
class TestUserLookup:
    def setup(self):
        self.repo = UserRepository(MockDB())

    def test_find_by_id(self):
        self.repo.db.add_user(id=1, name="Alice")
        assert self.repo.find_by_id(1)["name"] == "Alice"
```

Here:
- Multiple methods share the `db` dependency (justified class)
- A test double exists (concrete second use case for the interface)
- The call site is simpler (`repo.find_by_id(1)` vs repeating the query string)

No FLOW signal. The abstraction earns its cost.
