# N+1 Query Detection — "Query in a loop"

Calibrates: N+1 query pattern detected. `n_plus_one_query` fires at weight 2.0.

---

## Input

> Write a function that returns user profiles with their recent orders.

```python
def get_user_profiles_with_orders(user_ids):
    profiles = []
    for user_id in user_ids:
        user = db.query("SELECT * FROM users WHERE id = ?", user_id)
        orders = db.query("SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", user_id)
        profiles.append({
            "user": user,
            "recent_orders": orders
        })
    return profiles
```

---

## Trigger Gate

1. Retries / backoff / timeouts? — No.
2. Queues / streams / concurrency? — No.
3. Caches / memoization / invalidation? — No.
4. Startup / initialization? — No.
5. Hot paths / loops / scans? — **Yes.** A loop executing queries.
6. Build, test, CI, or dev workflow? — No.
7. Provider/API calls? — No.
8. Database or filesystem access? — **Yes.** Database queries in a loop.
9. Complex abstractions? — No.
10. Long-lived maintenance burden? — No.

**Triggers present: hot paths/loops, database access. FLOW runs. Principles to evaluate: External I/O Discipline, Hot-Path Awareness.**

---

## Efficiency Pass

### External I/O Discipline

The function executes two queries per user, inside a loop over `user_ids`. For N users, this is 2N queries. This is a classic N+1 query pattern — one query per item in a loop where a batched query would suffice.

The `users` query can be replaced with a single `WHERE id IN (...)` query. The `orders` query can be replaced with a single query that fetches recent orders for all users at once, then groups them in Python.

**Signals:**
- `n_plus_one_query` (2.0) — an N+1 query pattern is being used; one query per item in a loop where a single batched query would suffice

The queries have no pagination issue (the orders query already has `LIMIT 5`). No sync blocking issue (this is a synchronous function, not an async path). No missing batching signal needed — `n_plus_one_query` covers the core issue.

### Hot-Path Awareness

The loop is O(N) in the number of users, but each iteration does 2 DB queries. The algorithmic complexity is O(N) in Python but O(N) in DB round-trips, which is the dominant cost. The `algorithmic_drag` signal could fire, but `n_plus_one_query` (2.0) already covers this — the drag is the query pattern, not the Python loop. Suppress per stacking rules.

---

## Gate

W_flow = 2.0 (single signal, weight 2.0) >= 1.5 → Surface mode.

---

## SISPIS Integration

`n_plus_one_query` maps to:
- `downstream_impact` +2 (capped at 2.0)
- `tradeoff_density` +2 (capped at 2.0)

E elevates by +4 total. Likely pushes E past 6 → SISPIS Stage 1 hard override → SCHEMA mode.

---

## Output

```
**External I/O Discipline:** The function executes 2 queries per user in a loop — for 100 users, that's 200 database round-trips where 2 would suffice.

def get_user_profiles_with_orders(user_ids):
    if not user_ids:
        return []

    placeholders = ",".join("?" * len(user_ids))

    users = db.query(
        f"SELECT * FROM users WHERE id IN ({placeholders})",
        *user_ids
    )
    user_map = {u["id"]: u for u in users}

    orders = db.query(
        f"""SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC) as rn
            FROM orders WHERE user_id IN ({placeholders})
        ) WHERE rn <= 5""",
        *user_ids
    )

    orders_by_user = {}
    for order in orders:
        orders_by_user.setdefault(order["user_id"], []).append(order)

    return [
        {"user": user_map[uid], "recent_orders": orders_by_user.get(uid, [])}
        for uid in user_ids
    ]
```

The fix reduces 2N queries to 2 queries, regardless of N. The database does the filtering and limiting; Python does the grouping.

---

## What This Calibrates

`n_plus_one_query` is a 2.0-weight signal because it scales with traffic. 100 users → 200 queries. 1000 users → 2000 queries. The database can handle 2 queries efficiently; 2000 queries in a loop create connection pool exhaustion, lock contention, and latency that compounds.

The distinction FLOW draws: the N+1 pattern is not "simpler" — it is simpler to write and slower to run. The batched version is more code but fewer round-trips. The simplicity of the loop version is borrowed against operational cost that scales with the user base.

The fix uses `WHERE id IN (...)` for users and a window function (`ROW_NUMBER()`) for per-user order limiting. This is standard SQL — not clever, not micro-optimized. It is the correct pattern for batched fetching.

Anti-pattern to avoid: "It's fine for now, N is small." N grows. The N+1 pattern is in production before anyone notices, and the first symptom is a slow page load under real traffic. Batch from the start.

---

## ANCHOR Integration

If the user accepts the N+1 pattern as a tradeoff (e.g., "we'll fix it later, N is always under 10"), ANCHOR's Action Accountability records:
- Action: N+1 query pattern accepted
- Reason: N is bounded and small (< 10)
- Evidence: user stated N is always under 10
- Outcome: 2N queries accepted for now
- Next State: re-evaluate if N exceeds 10 or if performance issues arise

DOX's closeout pass would note this accepted tradeoff in the relevant AGENTS.md.
