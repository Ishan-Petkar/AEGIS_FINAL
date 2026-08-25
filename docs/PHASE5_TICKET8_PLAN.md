# Ticket #8 Plan — FastAPI REST routes

Planning authority: this document. Implementation must follow the decisions
here; where it disagrees with a decision, raise it rather than silently
diverging. Contract source: `docs/PHASE5_BUILD_PLAN.md` §7.

---

## 1. Scope

**In scope — nine routes:**

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | liveness + per-component honesty |
| GET | `/api/topology` | nodes + edges from `graph_manager.build_graph()` |
| GET | `/api/events?limit=&since=` | recent events, paged |
| GET | `/api/alerts?acknowledged=` | alert list |
| POST | `/api/alerts/{id}/ack` | acknowledge |
| GET | `/api/cii/{asset}` | on-demand blast radius |
| POST | `/api/replay/start` | `{dataset, speed}` |
| POST | `/api/replay/stop` | — |
| POST | `/api/replay/speed` | `{multiplier}` |

**Explicitly OUT of scope — do not implement:**

- `POST /api/inject` — Ticket #13.
- `WS /ws/stream` — Ticket #9. Ticket #7 already defined the `Broadcaster`
  protocol and the envelope payloads it will carry; do not anticipate it.
- `GET /api/stats` — Ticket #16 owns it (`docs/PHASE5_BUILD_PLAN.md` §9).
  `IngestPipeline.stats()` already exists for it; leave it alone.

---

## 2. Files

| File | Role |
|---|---|
| `backend/main.py` | app factory, lifespan, middleware, router mount |
| `backend/runtime.py` | process-wide `AppRuntime` (engine, pipeline, scorer) |
| `backend/schemas.py` | Pydantic request/response models |
| `backend/routes.py` | the nine route handlers, one `APIRouter` |
| `tests/test_api.py` | new tests |

Additive edits only to `backend/config.py` (new settings, §7 below).
**`src/` is untouched — Invariant A.**

---

## 3. Decision: runtime wiring (D8-1)

The replay-control routes need *the* running `ReplayEngine`, and
`/api/health` needs to know whether the scorer loaded. These are
process-wide objects with real lifecycles, so:

- `backend/runtime.py` defines `AppRuntime` holding `engine`,
  `pipeline`, `scorer`, and `started_at`.
- It is constructed in the **lifespan**, never at import time. Importing
  `backend.main` must not load a joblib artifact, open a DB pool, or read
  a dataset — that would make the test suite and any CLI import expensive
  and order-dependent.
- Stored on `app.state.runtime`; routes reach it through a
  `get_runtime()` FastAPI dependency so tests can override it.
- Lifespan shutdown calls `engine.stop()` (idempotent per its docstring).

**Scorer load must fail loudly.** `StreamingScorer.load()` deliberately
hard-fails rather than refitting (that refit is exactly the Invariant B
violation this phase exists to prevent — see K7). If the artifact is
missing, the lifespan records the failure on the runtime and `/api/health`
reports it; the app still starts so the read-only routes work, but the
replay routes return **503**, never a silent no-model stream.

---

## 4. Decision: `since` is an event id, not a timestamp (D8-2)

**This is the ticket's one subtle correctness decision.** The state board's
"Note for Ticket #8" records that hundreds of events share one `ts` on
minute-granularity days (friday-morning: median 629 per bucket, max
4,017). Therefore:

- **Ordering is `ORDER BY ts DESC, id DESC`** — mandatory, not optional.
  `ts` alone is ambiguous within a bucket and makes the live feed jumble.
- **`since` is an exclusive event-id lower bound** (`WHERE id > :since`),
  **not** a timestamp. A timestamp cursor inside a 4,017-row bucket either
  re-delivers the whole bucket or skips part of it; there is no correct
  timestamp cursor at this granularity. The serial `id` is exact because
  `IngestPipeline` inserts each batch in arrival order (Ticket #7).

Document this in the route docstring and the OpenAPI field description —
a frontend author will otherwise reasonably assume `since` takes a time.

> **Correction (post-review, HIGH-1 — see docs/PHASE5_STATE.md decision
> P5-18).** The bullet above — "Ordering is `ORDER BY ts DESC, id DESC`,
> mandatory, not optional" — is **incomplete as originally written**. It
> is correct for a page load (`since` omitted) and wrong for a catch-up
> poll (`since` supplied). The two were never distinguished here, and
> combining a `since`-bounded query with a DESC ordering silently and
> permanently loses events: `WHERE id > :since ORDER BY ts DESC, id DESC
> LIMIT n` returns the *newest* `n` matching rows, not the *next* `n`
> after the cursor. A client polling forward from `since` skips everything
> older than that newest page and can never come back for it — proven on
> real Postgres (250 tied-`ts` events, ids 5067..5316, cursor at 5067,
> `limit=100`): poll 1 returned ids 5217..5316 and advanced the cursor to
> 5316; every following poll returned 0 rows; **149 of 249 events were
> never delivered**, with no signal in the response that anything was
> missed.
>
> This plan's D8-2 did not consider the catch-up case at all — it treated
> "ordering" as one global property of the route rather than a property of
> *which query the route is actually being asked to run* (a bounded
> forward drain vs. an unbounded backward "latest N"). That gap is what
> produced the bug; it was not an implementation deviation from this plan.
>
> **Superseded by:** ordering is now conditional on `since`.
> `since` omitted -> `ORDER BY ts DESC, id DESC` (unchanged, correct for
> "show me the latest N"). `since` supplied -> `ORDER BY ts ASC, id ASC`
> (new), so the client drains its backlog forward with no gap. The `id`
> tie-break from the original decision is preserved in both directions —
> that part of D8-2 was correct and remains load-bearing. The response
> envelope also gained `has_more: bool` (computed by fetching `limit + 1`
> rows and trimming), so a client can tell it is still behind. See
> `backend/routes.py`'s `list_events` docstring and the `since` field
> description for the full rationale, and
> `tests/test_api.py::test_events_catch_up_polling_drains_every_tied_timestamp_event_with_no_gap`
> for the regression test (verified to fail under the original DESC-only
> behaviour).

> **Second correction (post-review, HIGH-2 — see docs/PHASE5_STATE.md
> decision P5-18's addendum).** The HIGH-1 fix above did not go far
> enough. It made the catch-up branch ascending, but left it sorted by
> `ts ASC, id ASC` while the filter stayed `WHERE id > :since` — the
> filter key and the sort key are different columns. That is only safe if
> `id` order and `ts` order always agree, and they do not: replaying the
> same capture day a second time restarts the replay's own virtual clock,
> so the second session's rows get HIGHER ids than the first session's
> but can land in an EARLIER or overlapping `ts` range. Measured on real
> Postgres, three replay sessions of the same day: session A ids
> 5062..5066 (ts 14:29..14:30), session B ids 5567..11566 (ts
> 14:29..14:41), session C id 11567 alone (ts 14:29 — the earliest `ts`
> of the three sessions, but the highest id). `since=10567&limit=5` under
> `ts ASC, id ASC` sorted id 11567 to the front of the page (earliest
> ts), so a client advancing its cursor to `max(id)` jumped straight to
> 11567 and permanently skipped ids 10568..11566; a client advancing to
> the last row in *sort* order instead re-received id 11567 on every
> following poll forever (`11567 > cursor` always holds), an infinite
> duplicate loop. Draining from a cursor below every event: **200 of 6006
> events delivered — 96.7% silently lost** — worse than the HIGH-1 bug
> this same route had one fix round for already.
>
> **Principle:** cursor pagination is gapless only when the filter key
> and the sort key are the *same* column. HIGH-1 fixed the direction
> (ascending vs. descending) but not this.
>
> **Superseded by:** the catch-up branch (`since` supplied) now orders by
> `ORDER BY id ASC` ALONE — no `ts` in the sort at all — so the filter key
> and the sort key are identical and the drain is gapless by
> construction. This also matches docs/PHASE5_STATE.md's "Note for Ticket
> #8": the serial `id` preserves true emission order within a batch, and
> event-time ordering is a page-load concern, not a catch-up one. The
> page-load branch (`since` omitted) is unchanged: `ORDER BY ts DESC, id
> DESC`. See `backend/routes.py`'s `list_events` docstring and the
> `since` field description for the full rationale, and
> `tests/test_api.py::test_events_catch_up_survives_id_and_ts_order_disagreement`
> for the regression test (verified to fail under the `ts ASC, id ASC`
> catch-up ordering: a fixture with a later-timestamp batch inserted
> first and a single earliest-timestamp row inserted last, so id order
> and ts order disagree, produced a drain that delivered only 3 of 6
> events after the seed and then stopped, permanently skipping the rest).

---

## 5. Decision: `/api/cii/{asset}` 404s on an unknown asset (D8-3)

`CLAUDE.md` §7 records as a **known issue** that the dashboard's What-If
selectbox offers assets absent from `DEPENDENCY_GRAPH` and "returns an
empty `CIIResult()` (all zeros) rather than an error". The API must not
reproduce that bug.

- Asset **not** a node in `build_graph()` → **404** with a message naming
  the asset. Never a 200 carrying fabricated zeros.
- Membership test is the same one Ticket #7 established (P5-17): presence
  in the criticality map built from `compute_seed_rows()`. Reuse
  `backend.ingest.build_criticality_map()` — do not build a second one
  (Invariant D, one graph authority).
- Optional `?anomaly_score=` (float, `0 < x <= 1`, default **1.0**).
  Default 1.0 = "if this asset were fully compromised", which is the
  question an operator asks on demand.

---

## 6. Decision: CORS is required, and must be a setting (D8-4)

The Next.js console (Ticket #3) runs on `localhost:3000`; this API runs on
`localhost:8000`. Different origin ⇒ **every** browser call fails without
CORS, and Ticket #12 (mock → real WS swap) would be blocked by it. Add
`CORSMiddleware` driven by a new setting, defaulting to the two localhost
:3000 forms. Do not default to `["*"]`: these endpoints are
unauthenticated state-changing controls (see `api_host`'s own docstring),
and `*` plus a wide bind address hands the LAN the demo.

---

## 7. New `BACKEND_SETTINGS` fields

Follow the existing conventions exactly: `Field(...)` with bounds and a
description that says *why*, and the optional-override pattern for any
function that consumes them.

| Field | Default | Purpose |
|---|---|---|
| `api_cors_origins` | `["http://localhost:3000", "http://127.0.0.1:3000"]` | D8-4 |
| `api_events_default_limit` | `100` | page size when `limit` omitted |
| `api_events_max_limit` | `1000` | hard cap; an unbounded `limit` lets one request pull the whole 500k-row retention window |
| `api_alerts_default_limit` | `100` | same reasoning, alerts panel |

---

## 8. Route-by-route requirements

**`GET /api/health`** — returns `status` (`ok` | `degraded`), plus booleans
`database`, `scorer_loaded`, `replay_running`, and `uptime_sec`. DB check
is a cheap `SELECT 1`, wrapped so an unreachable DB yields `degraded`, not
a stack trace. **503** when `status == "degraded"`, 200 otherwise.

**`GET /api/topology`** — nodes and edges from `build_graph(directed=True)`.
Each node carries `name`, `criticality`, `type`, `purdue_level`,
`is_gateway`; each edge carries `source`, `target`, `edge_type`, `prob`,
`is_gateway_edge`. Node metadata comes from `compute_seed_rows()`, not a
second hand-rolled lookup. No DB required — this must work before any
seeding has happened.

**`GET /api/events`** — §4 above. `limit` bounded by
`api_events_max_limit`; out-of-range → 422 via Pydantic `Query` bounds,
not a manual clamp (a silent clamp lies to the caller).

**`GET /api/alerts`** — optional `acknowledged` filter (`true`/`false`/
omitted = both). Ordered `ts DESC, id DESC`, using the existing
`ix_alerts_acknowledged_ts_desc` index. Bounded `limit`.

**`POST /api/alerts/{id}/ack`** — 404 if absent. **Idempotent**: acking an
already-acked alert must **not** overwrite the original
`acknowledged_at` — the first acknowledgement is the operator record.
Returns the alert.

**`POST /api/replay/start`** — body `{dataset, speed}`; map `dataset` →
the engine's `day` parameter (the contract and the engine disagree on the
name; the contract wins at the boundary). Also accept optional `limit` and
`start_at`, both of which `ReplayEngine.start()` already supports and the
demo wants (friday-morning's first attack is at 09:34). `start()` raises
`ReplayEngineError` when already running — that is **409 Conflict**, not
500, and deliberately not a silent no-op (the engine's docstring is
explicit about why). 503 if the scorer failed to load (§3).

**`POST /api/replay/stop`** — idempotent, 200 even when not running.
Returns the final status.

**`POST /api/replay/speed`** — body `{multiplier}` with Pydantic `gt=0`,
so a non-positive value is 422 before reaching the engine. 409 if not
running.

All three replay routes return a `ReplayStatusResponse` built from
`engine.status()` so the client always sees authoritative state.

---

## 9. Tests (`tests/test_api.py`)

- **Default suite must not require Postgres.** Health/topology/replay
  routes need no DB. DB-backed routes get their session dependency
  overridden with a fake in the default suite; the real round-trip is
  covered by live-DB tests gated on `AEGIS_TEST_LIVE_DB=1`, matching
  `tests/test_backend_models.py` and `tests/test_ingest.py`.
- Use `fastapi.testclient.TestClient` (httpx 0.28 is installed).
- **Required assertions, at minimum:**
  - events ordering is `ts DESC, id DESC` — assert against a fixture with
    tied timestamps, or this test proves nothing;
  - `since` filters by id and is exclusive;
  - `limit` above `api_events_max_limit` → 422 (not a silent clamp);
  - `/api/cii/{unknown}` → 404, and the body does not contain zeros
    masquerading as a result;
  - `/api/cii/{known}` → 200 with median/p5/p95;
  - ack is idempotent and preserves the first `acknowledged_at`;
  - `replay/start` twice → 409;
  - `replay/speed` with `0` and `-1` → 422;
  - importing `backend.main` performs no artifact load and no DB
    connection (guard the lifespan decision in §3).

---

## 10. Gates (all must pass before handing back)

```bash
PYTHONPATH=src python -m pytest tests/ -q          # no regressions, 448 + new
ruff check src/ backend/ --select E,F,W --ignore E501
git status --short src/                             # MUST be empty (Invariant A)
```

Report the real numbers. If something fails, say so with the output rather
than describing the intent.
