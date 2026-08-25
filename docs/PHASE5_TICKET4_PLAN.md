# Ticket #4 Plan — Mock WebSocket server + stream client

Planning authority: this document. `docs/DESIGN_CONSOLE.md` governs all
visuals. Raise disagreements rather than diverging silently.

---

## 1. Why this ticket exists

`docs/PHASE5_BUILD_PLAN.md` §9 calls `#4` "the load-bearing scheduling
decision: it ships Day 1 so frontend work never blocks on backend
readiness." Tickets #10 (live feed) and #11 (graph) both need a stream of
realistic envelopes; the real `WS /ws/stream` is Ticket #9 and does not
exist yet.

A second reason, learned in Ticket #3's review: a mock the developer can
**kill and restart at will** is the only practical fixture for testing
reconnect behaviour. That review found the console displaying
contradictory connection states, and it was only caught by killing the
backend mid-session. The mock makes that test cheap and repeatable.

---

## 2. Scope

**In scope:**
1. `frontend/scripts/mock-ws-server.mjs` — standalone WebSocket server on
   **port 8001**, path **`/ws/stream`**, emitting the envelope shapes
   Ticket #7 already defined.
2. WebSocket envelope **TypeScript types** added to
   `frontend/src/lib/types.ts`, derived field-by-field from
   `backend/ingest.py`.
3. `frontend/src/lib/useEventStream.ts` — the stream client hook:
   connect, parse, expose latest envelopes, auto-reconnect with backoff.
4. Minimal visible proof: the header's `events/s` and `alerts` stat chips
   go live, and a stream-status indicator distinguishes *API* reachability
   from *stream* connectivity.
5. `npm run mock` script and `NEXT_PUBLIC_WS_URL` in
   `frontend/.env.local.example`.

**OUT of scope — deliberately left to their own tickets:**
- The telemetry feed's own behaviour — autoscroll, 200-row buffer cap,
  row rendering. **Ticket #10.** `TelemetryRail` keeps its static
  placeholder rows in this ticket.
- The force-directed graph and node pulsing. **Ticket #11.**
- Alert ack wiring. **Ticket #15.**
- The real server-side `WS /ws/stream`. **Ticket #9.**
- Swapping the frontend from mock to real. **Ticket #12.**

The boundary is: **#4 delivers the transport and proves it moves data;
#10 and #11 render it properly.**

---

## 3. Decision: the mock is a drop-in for the real endpoint (D4-1)

Ticket #12's job must be a **URL change and nothing else**. Therefore:

- Path is `/ws/stream` — identical to what §7 of the build plan specifies
  for the real server. Do not invent a different path.
- Envelope shape is `{"type": ..., "data": {...}}` with `type` in
  `event | alert | cii` — exactly `ENVELOPE_EVENT` / `ENVELOPE_ALERT` /
  `ENVELOPE_CII` in `backend/ingest.py`.
- Port **8001**, so the mock and the real backend (8000) can run at the
  same time without a conflict. The client picks its target from
  `NEXT_PUBLIC_WS_URL`, defaulting to `ws://127.0.0.1:8001/ws/stream`.

**Field parity is a hard requirement, not a nicety.** Copy the payload
keys verbatim from `backend/ingest.py`:

- `event.data`: `id, ts, observed_at, source_ip, destination_ip,
  source_asset, destination_asset, protocol, bytes, packets,
  duration_sec, raw_score, calibrated_score, is_anomaly, tripwire_fired,
  confidence, replay_session_id, batch_index`
- `alert.data`: `id, ts, severity, asset, title, detail, explanation,
  cii_snapshot_id, acknowledged`
- `cii.data`: `snapshot_id, origin_asset, cii_median, cii_p5, cii_p95,
  impacted, trigger_event_id`

A mock that emits a field the real server does not (or omits one it does)
guarantees Ticket #12 breaks in a way that looks like a frontend bug. In
your report, state explicitly that you diffed these against
`backend/ingest.py` and what the result was.

---

## 4. Decision: the mock emits *plausible* data, not random noise (D4-2)

The point is to build the UI against data shaped like reality, so:

- **Asset names** come from the real topology — the curated node names in
  `config.SMART_CITY_ASSETS` / `build_graph()` (`City_Payment_Gateway`,
  `Traffic_Controller`, `SCADA_Historian`, `Bank_Partner_API`, gateways).
  Fetch `GET /api/topology` from the real backend on mock startup when it
  is reachable; fall back to a small hardcoded list when it is not (the
  mock must run standalone — that is its entire purpose).
- **IPs** mirror reality: mostly `192.168.10.x` and `10.0.1.x`, with the
  occasional external address. K8 on the state board records that real
  CIC-IDS2017 traffic is `192.168.10.x`.
- **Anomaly rate** is low by default (~2%), matching the real signal
  density rather than a christmas tree of alerts.
- **Alerts are rare** and mostly tripwire-severity `critical`, consistent
  with the alert policy Ticket #7 implemented (P5-15): volumetric
  anomalies are suppressed by default, so an alert per anomaly would
  misrepresent the system.
- A `cii` envelope follows an alert, as it does in the real pipeline.
- Rate configurable via `--rate` (events/sec, default ~8) and `--seed`
  for reproducibility.

---

## 5. Decision: stream status is separate from API status (D4-3)

Ticket #3 established a `ConnectionProvider` owning the `/api/health`
poll. The WebSocket is a **second, independent** transport: the REST API
can be healthy while the stream is down, and vice versa.

Do **not** collapse them into one boolean. Expose both, and make the
header show them distinctly. Ticket #3's review found that two components
disagreeing about "connected" is exactly what makes a UI look broken —
the fix there was one source of truth per concern, not one boolean for
everything.

Reconnect with **exponential backoff** (e.g. 1s → 2s → 4s → capped at
~15s), and never spin a tight reconnect loop. Any status copy must be
true — if it says "reconnecting", a reconnect must actually be scheduled.
That was a real defect in Ticket #3.

---

## 6. Implementation notes

- Node's `ws` package as a **devDependency** of `frontend/`. The mock is a
  dev tool; it must never be imported by application code or reach a
  production bundle.
- Plain `.mjs`, no build step. `npm run mock` must work from a clean
  checkout after `npm install`.
- Clean shutdown on SIGINT, and a startup banner naming the port, path,
  rate, and seed so the developer can see what they are getting.
- The client hook must clean up its socket and timers on unmount — a
  leaked socket surviving Fast Refresh will produce duplicate envelopes
  and waste hours of debugging.
- Guard against unbounded memory in the hook: keep a **bounded** rolling
  buffer, not an ever-growing array.

---

## 7. Verification (do it; report real output)

```bash
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
cd frontend && npm run build
```

Then prove it works, in a browser, with `mcp__Claude_Browser__*`:

1. Start the mock (`npm run mock`) and the dev server.
2. Confirm the header's `events/s` chip shows a **non-zero, changing**
   value and the stream indicator reads connected.
3. `read_network_requests` / `read_console_messages` — confirm the
   WebSocket connects and there are **no** console errors.
4. **Kill the mock.** Confirm the stream indicator goes disconnected
   *while the API indicator stays connected* (D4-3), and that no tight
   reconnect loop appears in the console.
5. **Restart the mock.** Confirm the stream recovers on its own within
   the backoff window, with **no page reload**.
6. Screenshot the live state.

Steps 4–5 are the acceptance bar, for the same reason they were in
Ticket #3: a recovery claim without evidence does not count.

Backend must remain untouched:
```bash
git status --short src/ backend/     # empty
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q   # 483 passed, 13 skipped
```

---

## 8. Constraints

- Do not modify `src/` or `backend/`. If the frontend appears to need a
  backend change, stop and report it.
- No raw hex or `rgba()` in components — tokens only.
- Do not commit `node_modules/` or build output.
- Stay inside §2's scope. The feed, graph, ack, and real WS are other
  tickets; leaving them alone is the plan working, not an omission.
