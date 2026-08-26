# Ticket #16 Plan — `GET /api/stats`

Planning authority: this document.

---

## 1. Scope

`GET /api/stats` — the header counters. `docs/PHASE5_BUILD_PLAN.md` §7
lists it as *"header counters (events/s, alerts, risk index)"*.

Most of this is plumbing: `IngestPipeline.stats()` already returns 12 real
counters (`batches, flows_received, events_inserted, events_deduplicated,
anomalies, tripwire_hits, cii_computed, cii_reused, alerts_created,
alerts_suppressed, broadcast_failures, events_pruned`).

The one part that is **not** plumbing is the risk index. Read §3.

**Out of scope:** README/SDG (#17/#18), styling (#19), the CII saturation
decision (recorded separately, user's call).

---

## 2. Response shape

Compose from three real sources, and say which is which:

- **Ingest counters** — from `IngestPipeline.stats()`.
- **Replay status** — from `runtime.engine.status()` (`emitted_count`,
  `total_for_day`, `current_virtual_position`, `lag_seconds`, `running`).
- **Alert counts** — real counts by severity and acknowledged state, from
  the database, not from the in-memory counter (an operator restart must
  not reset what the panel shows).

Return `503` if the runtime has no engine (scorer never loaded), matching
the replay routes. Bound any DB query.

---

## 3. Decision: the risk index must be defined, or absent (D16-1)

"Risk index" is undefined in the build plan. A number in a header labelled
`RISK` will be read as authoritative, so inventing one to fill the slot is
exactly the fabricated-signal problem refused in P5-15, D8-3, D11-1 and
D13-1.

**Define it explicitly, from real quantities, and surface the definition
in the UI** (tooltip/`title`), so nobody mistakes it for a standard
measure:

```
risk = clamp(0..100) of
       Σ over UNACKNOWLEDGED alerts of (severity_weight × asset_criticality)
       normalised against a configured full-scale constant
```

- `severity_weight`: critical > warning > normal, from a new setting.
- `asset_criticality`: the real value from the graph authority
  (`build_criticality_map()`), so an alert on the payment gateway counts
  for more than one on a traffic camera.
- Unacknowledged only — acknowledging an alert should visibly reduce
  operator risk, which is what makes the number behave like an operator
  tool rather than a decoration.
- Full-scale constant lives in `BACKEND_SETTINGS` with a docstring stating
  it is a **presentation scale, not a calibrated probability**.

**Do NOT build the risk index on CII.** Measured this session across all
50 assets: 28 report exactly 0.0 and 18 exactly 1.0, with only 4 in
between — CII is currently near-binary, and feeding it into a headline
index would propagate that degeneracy into the number an operator reads
first. Record this reasoning in the code comment.

**If there are no unacknowledged alerts the index is `0`, not `—`** —
zero risk is a real, meaningful state. `—` is reserved for "no basis to
compute", e.g. no engine.

---

## 4. Decision: one authoritative source per metric (D16-2)

The frontend already computes `events/s` client-side from the WebSocket.
If `/api/stats` also returns an `events/s`, the header can show two
numbers that look like the same metric and disagree — the exact defect
class already hit twice here (Ticket #3's header vs panel contradiction,
Ticket #10's duplicate sockets).

So:
- **Server owns cumulative totals** (events ingested, anomalies, alerts,
  tripwire hits).
- **Client owns the live per-second rate** of what *it* received, and is
  labelled as such.
- `/api/stats` may return a server-side rate **only** if it is labelled
  distinctly (e.g. `ingest_rate_per_sec`) and the UI never renders both as
  "events/s".

---

## 5. Decision: surface `alerts_suppressed` (D16-3)

`alerts_suppressed` counts volumetric anomalies that were detected,
scored, persisted and broadcast but deliberately did **not** page an
operator (P5-15, because the volumetric channel measured ~0.02 precision).

Showing it makes the alert policy visible instead of hidden — the system
is transparently saying "I saw N of these and chose not to wake you."
That is a credibility feature, not noise. Render it somewhere honest
(e.g. alongside the alert counts), with a tooltip explaining why.

---

## 6. Frontend

Wire the header's `RISK` chip to the real index, with the definition in a
`title` tooltip. Add the cumulative totals where they fit the existing
header/strip design. Poll at a modest interval (reuse the existing
connection context's cadence rather than adding a second timer).

Do not regress: one WebSocket per tab, sector graph + expand, sector
strip, replay progress, alerts panel, never-a-blank-panel, honest copy.

---

## 7. Verification

```bash
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q   # 518 baseline
./venv/bin/ruff check src/ backend/ --select E,F,W --ignore E501
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

Live:
1. `GET /api/stats` with no replay → sane zeros, `503` only if no engine.
2. Start a real replay → totals climb and match Postgres counts (cite both).
3. Inject a honeytoken → `alerts_created` and the risk index both rise.
4. **Acknowledge that alert → the risk index falls.** This is the
   behaviour that proves the index is wired to something real.
5. Confirm the UI never shows two different "events/s" numbers.
6. Zero console errors in a fresh tab.

---

## 8. Constraints

- `src/` must not change — the city scale-up is done; this is backend +
  frontend only.
- New settings follow the existing `BACKEND_SETTINGS` convention (bounds
  plus a docstring saying *why*).
- Do not fabricate any metric. If a number has no real basis, omit it.
- Commit nothing until the suite is green.
