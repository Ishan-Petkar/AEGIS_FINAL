# Ticket #9 Plan — real WebSocket endpoint (`WS /ws/stream`)

Planning authority: this document.

**Resequenced ahead of Ticket #10** on an explicit instruction that no
synthetic data may appear anywhere in the product. Building the live feed
against the Ticket #4 mock would have baked synthetic events into the
demo path; this ticket makes the real stream — real replayed
CIC-IDS2017 traffic — available first, so #10 renders real data from the
start. This also restores Invariant E ("real telemetry on landing, not
fabricated").

---

## 1. Scope

**In scope:**
1. `WS /ws/stream` on the FastAPI app.
2. A `WebSocketBroadcaster` implementing the existing `Broadcaster`
   protocol (`backend/ingest.py:140`, `publish(envelope) -> None`).
3. Wiring it into `AppRuntime` so `IngestPipeline` broadcasts to
   connected clients.
4. Frontend default flipped from the mock (`:8001`) to the real endpoint
   (`:8000/ws/stream`). **This absorbs Ticket #12's swap** — the mock
   remains available but becomes opt-in via `NEXT_PUBLIC_WS_URL`, used
   only as a reconnect-test fixture, never as a data source.
5. Tests.

**OUT of scope:** feed rendering (#10), graph (#11), `/api/stats` (#16),
inject (#13).

---

## 2. The hard part: a sync thread publishing into an async loop (D9-1)

`IngestPipeline._safe_publish()` calls `broadcaster.publish(envelope)`
**synchronously, from the `ReplayEngine`'s background thread**
(`backend/replay_engine.py` runs its loop in a `threading.Thread`).
FastAPI/Starlette WebSockets are asyncio objects owned by the event loop.
**Touching a WebSocket from that thread is a race and will corrupt the
connection or raise.**

Required approach:

- Capture the running event loop **once, in the lifespan**
  (`asyncio.get_running_loop()`), and store it on the broadcaster.
- `publish()` must be **non-blocking and thread-safe**: hand the envelope
  to the loop via `loop.call_soon_threadsafe(...)` (or
  `run_coroutine_threadsafe` without ever calling `.result()`), then
  return immediately.
- `publish()` must **never** `await`, never block, and never raise into
  the ingest thread. `_safe_publish` already catches exceptions and
  counts them, but a blocking publish would stall the replay engine and
  desynchronise the whole stream — that is a correctness bug, not just
  slowness.
- Guard the case where no loop is set (e.g. the pipeline is driven
  outside the API process, as the Ticket #7 tests do): degrade to a no-op
  with a counter, never crash.

---

## 3. Backpressure: a slow client must never slow the replay (D9-2)

Ticket #7 publishes **one envelope per event**. At the demo's 20× replay
that is a steady stream; at higher speeds it is hundreds per second
(P5-12 measured `speed=2000x` producing 500-flow batches). A browser tab
that cannot keep up must not become the replay engine's rate limiter.

Required:

- **Per-client bounded queue** (`asyncio.Queue` with a `maxsize` from a
  new `BACKEND_SETTINGS` field, default 1000).
- On overflow, **drop the oldest** envelope for that client and increment
  a per-connection `dropped` counter. Dropping is the correct behaviour
  for a live view — a stale event is worthless — but it must be
  **counted and logged**, never silent.
- One writer task per connection draining its own queue. A dead or slow
  client affects only its own queue.
- Broadcast is fan-out by copy of the envelope reference; never mutate an
  envelope after publishing it.

---

## 4. Connection lifecycle (D9-3)

- Accept, register, and send an immediate **hello/snapshot** frame so a
  freshly-connected client is not staring at nothing until the next
  event: include current replay status (from `runtime.engine.status()`)
  so the UI can render state on connect.
- Remove the client on `WebSocketDisconnect`, on send failure, and on
  server shutdown. Cancel its writer task and drain its queue.
- Shutdown in the lifespan must close all sockets cleanly — an orphaned
  writer task surviving reload is a real leak.
- Never let one client's exception propagate and kill the endpoint for
  everyone.

---

## 5. Envelope contract — already fixed, do not redesign

`backend/ingest.py` already emits `{"type": "event"|"alert"|"cii",
"data": {...}}` with exact payload keys. Ticket #4 mirrored them, and the
frontend's `types.ts` is built on them. **This ticket must not change any
payload shape.** It only transports what ingest already produces.

A `hello` frame is the one addition; give it its own `type` so existing
client handling is unaffected by an unknown type.

---

## 6. New `BACKEND_SETTINGS` fields

Follow the existing convention (bounds + a description saying *why*):

| Field | Default | Purpose |
|---|---|---|
| `ws_client_queue_max` | `1000` | per-client bounded queue (D9-2) |
| `ws_send_timeout_sec` | `5.0` | a stuck send must not pin a writer task forever |

---

## 7. Tests (`tests/test_websocket.py`)

Default suite must not need Postgres or a real replay. Use
`fastapi.testclient.TestClient`'s WebSocket support with a fake runtime.

Required assertions:
- A connected client receives an envelope published through the
  broadcaster.
- **`publish()` from a non-main thread reaches a connected client** —
  this is D9-1's whole point; drive it from an actual
  `threading.Thread`, not the test thread, or the test proves nothing.
- `publish()` returns promptly and does not raise when no client is
  connected, and when no loop is set.
- Queue overflow drops oldest and increments the counter rather than
  blocking or raising.
- Disconnect removes the client; a second client keeps receiving.
- Payload shapes are unchanged (guard against accidental redesign).

---

## 8. Verification

```bash
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q   # 483 + new, no regressions
./venv/bin/ruff check src/ backend/ --select E,F,W --ignore E501
git status --short src/                                 # empty (Invariant A)
```

Then prove it **with real data end to end** — this is the acceptance bar:

1. Start the API. Connect a WebSocket client to `ws://127.0.0.1:8000/ws/stream`.
2. `POST /api/replay/start` with the real `friday-morning` dataset.
3. Confirm the client receives **real** `event` envelopes whose
   `source_dataset` is `CIC-IDS2017-TrafficLabelling` and whose IPs are
   real capture addresses — not synthetic.
4. Confirm `consumer_error_count` stays 0 and the replay is not slowed by
   the WebSocket.
5. Point the browser console at the real endpoint and confirm the header
   goes live on real traffic.
6. Report real observed numbers (events received, drops, lag).

---

## 9. Constraints

- `src/` untouched (Invariant A).
- Invariant B: no model fit anywhere in this path.
- Do not change any existing envelope payload.
- The mock stays in the repo as a test fixture but must no longer be the
  frontend default.
