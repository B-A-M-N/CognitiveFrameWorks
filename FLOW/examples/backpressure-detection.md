# Backpressure Detection — "Unbounded queue"

Calibrates: unbounded queue detected. `unbounded_accumulation` fires at weight 2.0.

---

## Input

> Add a buffer that collects events from the webhook and processes them asynchronously.

```python
import asyncio

event_buffer = asyncio.Queue()

async def webhook_handler(request):
    event = await request.json()
    await event_buffer.put(event)
    return web.json_response({"status": "accepted"})

async def event_processor():
    while True:
        event = await event_buffer.put(event)
        await process_event(event)
```

---

## Trigger Gate

1. Retries / backoff / timeouts? — No.
2. Queues / streams / concurrency? — **Yes.** An `asyncio.Queue` is being used as a buffer.
3. Caches / memoization / invalidation? — No.
4. Startup / initialization? — No.
5. Hot paths / loops / scans? — No.
6. Build, test, CI, or dev workflow? — No.
7. Provider/API calls? — No.
8. Database or filesystem access? — No.
9. Complex abstractions? — No.
10. Long-lived maintenance burden? — No.

**Triggers present: queues/streams/concurrency. FLOW runs. Principle to evaluate: Backpressure.**

---

## Efficiency Pass

### Backpressure

`asyncio.Queue()` is created with no `maxsize` argument. This means the queue is unbounded — it will accept events without limit. If the webhook receives events faster than `event_processor` can process them, the queue grows without bound, consuming memory until the process is killed by OOM.

The webhook handler (`webhook_handler`) puts events into the queue but never checks whether the queue is full. The producer doesn't react to consumer capacity.

Additionally, the `event_processor` has a bug: `await event_buffer.put(event)` should be `await event_buffer.get()`. But that's a correctness issue for OWL/verification — FLOW's concern is the unbounded accumulation.

**Signals:**
- `unbounded_accumulation` (2.0) — the queue has no size limit; under load, memory grows without bound
- `missing_flow_control` (1.0) — the producer doesn't react to consumer slowness

Per stacking rules: `unbounded_accumulation` (2.0) is the higher-weight signal. `missing_flow_control` (1.0) is from the same principle and the same observation (the unbounded queue). The unbounded accumulation implies missing flow control. Suppress `missing_flow_control`.

**Final signal:** `unbounded_accumulation` (2.0)

---

## Gate

W_flow = 2.0 >= 1.5 → Surface mode.

---

## SISPIS Integration

`unbounded_accumulation` maps to:
- `downstream_impact` +2 (capped at 2.0)
- `tradeoff_density` +2 (capped at 2.0)

E elevates by +4 total. Likely pushes E past 6 → SCHEMA mode.

---

## Output

```
**Backpressure:** The event buffer is an unbounded asyncio.Queue — if webhooks arrive faster than the processor can handle them, memory grows without bound until the process is killed by OOM.

import asyncio

event_buffer = asyncio.Queue(maxsize=1000)

async def webhook_handler(request):
    event = await request.json()
    try:
        event_buffer.put_nowait(event)
        return web.json_response({"status": "accepted"})
    except asyncio.QueueFull:
        return web.json_response(
            {"status": "rejected", "reason": "buffer full"},
            status=503
        )

async def event_processor():
    while True:
        event = await event_buffer.get()
        await process_event(event)
```

The fix adds: `maxsize=1000` (bounds the queue), `put_nowait` with `QueueFull` handling (backpressure — when the buffer is full, the webhook returns 503 instead of accumulating unbounded memory), and correct `get()` in the processor.

---

## What This Calibrates

`unbounded_accumulation` is a 2.0-weight signal because it is a progressive memory leak that manifests under load. In testing, the queue never fills — events are processed as fast as they arrive. In production, a traffic spike or processor slowdown causes the queue to grow until the process dies.

The distinction FLOW draws: an unbounded queue is not "flexible" or "safe" — it is a memory leak with extra steps. The fix is a bounded queue with a defined overflow policy. The 503 response is not a failure — it is backpressure. The webhook caller can retry, and the system stays stable.

The overflow policy (reject with 503) is one option. Others include:
- Block the producer (backpressure propagates to the caller)
- Drop oldest events (bounded loss)
- Spill to disk (durable overflow)

The choice depends on the system's requirements. FLOW surfaces the unbounded accumulation; the specific overflow policy is a design decision.

Anti-pattern to avoid: "Use an unbounded queue so we never lose events." An unbounded queue loses ALL events when the process OOMs. A bounded queue with a defined policy loses events predictably and keeps the system running.

---

## OWL Integration

OWL's Verification principle would note the bug in `event_processor`: `await event_buffer.put(event)` should be `await event_buffer.get()`. This is a correctness issue — the processor would deadlock (putting into a queue it should be reading from). OWL surfaces this as a `contradiction` or `intent_deviation` — the code doesn't do what the function name implies.

FLOW and OWL fire on the same code for different reasons: OWL catches the correctness bug (wrong method), FLOW catches the operational hazard (unbounded queue). Both need fixing. The fixes are independent — the `get()` fix makes the processor work; the `maxsize` fix makes the system stable under load.
