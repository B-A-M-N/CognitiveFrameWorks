# Retry Storm Detection — "Retry loop without backoff"

Calibrates: retry loop without exponential backoff and jitter. `retry_storm_risk` fires at weight 2.0.

---

## Input

> Add retry logic to the API call so it retries on failure.

```python
def fetch_user(user_id):
    for attempt in range(5):
        response = requests.get(f"https://api.example.com/users/{user_id}")
        if response.status_code == 200:
            return response.json()
    raise Exception("Failed after 5 attempts")
```

---

## Trigger Gate

1. Retries / backoff / timeouts? — **Yes.** A retry loop is being added.
2. Queues / streams / concurrency? — No.
3. Caches / memoization / invalidation? — No.
4. Startup / initialization? — No.
5. Hot paths / loops / scans? — No.
6. Build, test, CI, or dev workflow? — No.
7. Provider/API calls? — **Yes.** An external API call is being made.
8. Database or filesystem access? — No.
9. Complex abstractions? — No.
10. Long-lived maintenance burden? — No.

**Triggers present: retries, provider/API calls. FLOW runs. Principles to evaluate: Retry Discipline, External I/O Discipline.**

---

## Efficiency Pass

### Retry Discipline

The retry loop uses `for attempt in range(5)` with no delay between attempts. Under load conditions where the API is degraded:
- All retrying clients hit the API simultaneously on each retry
- No backoff means retries fire as fast as the network round-trip allows
- No jitter means all clients retry in lockstep
- This creates a retry storm — synchronized retry traffic that prevents the API from recovering

The operation (GET request) is idempotent, so `non_idempotent_retry` does not fire.

The `requests.get()` call has no `timeout` parameter. If the API hangs, the call blocks indefinitely.

**Signals:**
- `retry_storm_risk` (2.0) — retry loop lacks exponential backoff with jitter; under load, synchronized retries will cascade
- `missing_timeout` (1.0) — the external call has no timeout; the call can hang indefinitely

Per stacking rules: `retry_storm_risk` (2.0) is the higher-weight signal. `missing_timeout` (1.0) is from the same principle and the same observation (the retry loop). Suppress `missing_timeout` — the storm risk implies the timeout is also missing.

**Final signal:** `retry_storm_risk` (2.0)

### External I/O Discipline

The API call is a single fetch — no N+1 pattern, no pagination issue. `requests.get()` is synchronous, but this is a single call, not a loop of calls. `sync_blocking_io` could fire, but the volume is trivial (1 call). No signal.

---

## Gate

W_flow = 2.0 (single signal, weight 2.0) >= 1.5 → Surface mode.

---

## SISPIS Integration

`retry_storm_risk` maps to:
- `downstream_impact` +2 (capped at 2.0)
- `tradeoff_density` +2 (capped at 2.0)

E elevates by +4 total. If base E was 2, it becomes 6 → SISPIS Stage 1 hard override → SCHEMA mode. The output must frame the retry storm as a decision, not just deliver a fix.

---

## Output

```
**Retry Discipline:** The retry loop uses no backoff or jitter — under load, all retrying clients will hit the API simultaneously on each attempt, creating a cascading retry storm that prevents recovery.

def fetch_user(user_id):
    for attempt in range(5):
        try:
            response = requests.get(
                f"https://api.example.com/users/{user_id}",
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
        except requests.RequestException:
            pass
        # Exponential backoff with full jitter
        sleep(random.uniform(0, min(2 ** attempt, 30)))
    raise Exception("Failed after 5 attempts")
```

The fix adds: `timeout=5` (prevents indefinite hang), exponential backoff with full jitter (prevents retry storm), and exception handling (retries on network errors, not just non-200 responses).

---

## What This Calibrates

`retry_storm_risk` is a 2.0-weight signal because it represents a cascading hazard — a pattern that compounds under load. A retry loop without backoff doesn't just create drag; it actively makes the system worse under the exact conditions (API degradation) that triggered the retry in the first place.

The fix is not "more complex" — it is the minimum viable retry. A bare `for` loop with `retry()` is not simpler; it is broken under load. Backoff with jitter is not an optimization; it is a correctness requirement for retry logic.

Anti-pattern to avoid: "Keep it simple — just retry 5 times." This is simple to write and catastrophic under load. The simplicity is borrowed against operational cost that manifests exactly when the system is already stressed.

---

## OWL Integration

OWL's Verification principle would ask: "What does done look like?" Done is not "retries 5 times." Done is "recovers from transient failures without cascading under load." The retry storm risk means the current implementation doesn't meet that criterion — it would fail under the exact conditions it's supposed to handle.
