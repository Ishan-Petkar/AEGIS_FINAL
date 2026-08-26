# Console Redesign Plan — rebalanced layout, sector graph, expand mode

Planning authority: this document. `docs/DESIGN_CONSOLE.md` still governs
colour, type, geometry, motion, and the accessibility floor.

---

## 0. Why this exists (read before changing anything)

Three consecutive rounds tried to make 50 labelled nodes legible inside a
**638×648px** canvas. That is **8,268 px² per node — a 91×91px cell** —
while a label like `City_Operations_Center` renders ~130px wide, *wider
than the whole cell*. The layout was never the problem; the geometry was
impossible. Do not attempt another positioning-algorithm fix at that size.

This plan fixes it by changing the two things that actually matter:
**how much space the graph gets**, and **how many nodes it draws at once.**

---

## 1. Target geometry (the acceptance numbers)

Work to these; they are computed, not guessed.

| View | Canvas | Nodes | px²/node | cell |
|---|---|---|---|---|
| Default (rebalanced, sector view) | ~780×600 | 11 sectors | ~42,500 | **206px** |
| Expanded (full-width, asset view) | ~1400×750 | 50 assets | ~21,000 | **145px** |

Both comfortably exceed the ~130px a full label needs. If the
implementation lands materially below these, the layout will fail again —
report it rather than shipping a cramped graph.

---

## 2. Permanent panel rebalance (D-R1)

Current: Telemetry 340 · **Graph 672** · Alerts 380 — the centrepiece
gets under half the width.

New default at ≥1280px:

```
┌────────────────────────────────────────────────────────────────┐
│ HEADER  AEGIS ·LIVE· events/s · alerts · risk · replay progress │  ~56px
├───────────┬────────────────────────────────────┬───────────────┤
│ TELEMETRY │  CITY INFRASTRUCTURE  [⤢ expand]   │ ACTIVE ALERTS │
│  ~280px   │  HERO — flex, ~780px               │    ~340px     │
├───────────┴────────────────────────────────────┴───────────────┤
│ SECTOR HEALTH STRIP — one chip per sector, full width          │  ~64px
└────────────────────────────────────────────────────────────────┘
```

Telemetry narrows to ~280px (rows are `time · src→dst · glyph`; they fit).
Alerts to ~340px. The graph takes the remainder.

---

## 3. Graph: sector view by default, assets on expand (D-R2)

**Default — sector nodes (~11).** Aggregate the 45 curated assets into
their sectors (Operations, Energy, Water, Transport, Public Safety,
Health, Telecom/IT, Finance, Civic, Environment, Monitoring).

- `City_Operations_Center` stays a **single node at the centre** — it is
  the hub, not a sector.
- Each sector node is **sized by asset count** and **coloured/badged by
  its worst current severity**.
- Sector→sector edges are derived by aggregating the real underlying
  asset edges (an edge exists if any asset edge crosses those sectors;
  weight = count). **Do not invent sector edges that no asset edge
  supports** — same rule as D11-1.
- Clicking a sector expands just that sector inline (its assets appear,
  others stay aggregated).

**Expanded — all 50 assets.** A maximise control (`⤢`) grows the graph to
the full window. This is the demo shot. All 50 assets, full labels, hub
centred, sectors as angular wedges. Escape / the same control returns.

Sector membership must be **derived from the data**, not a hardcoded name
list in the frontend — add a `sector` field to each asset in
`src/config.py` and surface it through `GET /api/topology`, so the
frontend never guesses. This is the one backend touch this plan allows.

---

## 4. More information on screen (D-R3)

Everything below must come from **real data already available**. Do not
invent a metric to fill space.

**Sector health strip (new, bottom).** One chip per sector:
name · asset count · worst severity dot · live event count for that
sector this session. Clicking a chip focuses that sector in the graph.

**Replay progress (header).** `ReplayStatus` already returns
`emitted_count` and `total_for_day` — render a real progress bar and the
current capture position (`current_virtual_position`). Right now an
operator cannot tell how far through the day the replay is.

**Alert severity counts (alerts panel header).** Real counts by severity
from the alert list, e.g. `3 critical · 1 warning · 12 acked`.

**Graph legend with counts.** `16 curated · 24 /24 clusters` already
exists — extend to show sector count and expanded/aggregated state.

**The `RISK` header chip is currently `—` and must stay `—`** unless it
is computed from something real. `/api/stats` is Ticket #16 and does not
exist yet. Do not fabricate a risk score to fill the gap.

---

## 5. Do not regress

Two-layer separation from the `/24` cluster layer and its honest caption;
the 24-cluster cap; curated nodes pinned/stationary; cascade animation
driven by the real `impacted` payload (never scripted) with median **and**
p5–p95; reduced-motion; one WebSocket per tab; never-a-blank-panel;
no-runaway-canvas; all copy literally true.

`frontend/src/components/AlertsRail.tsx` and `frontend/src/lib/api.ts`
hold **uncommitted, currently-working** Ticket #15 alert/ack code. Extend
them if needed for §4, but do not revert or rewrite them.

---

## 6. Verification (measure, don't assert)

```bash
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q   # 518 baseline
./venv/bin/ruff check src/ backend/ --select E,F,W --ignore E501
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

Live, at 1440×860 with a replay at **speed 20** (not thousands — high
rates make visual assessment harder):

1. **Report measured canvas px and px²/node for both views** against §1.
2. Default view: read every sector label off your own screenshot and list
   them. Hub visibly centred.
3. Expanded view: list every curated label you can actually read. A label
   truncated below ~8 characters is a fail.
4. Sector strip renders with real counts; clicking focuses the graph.
5. Replay progress advances against a real running replay.
6. Inject honeytoken → cascade still animates on real `impacted`.
7. Zero console errors in a fresh tab; four-width responsive intact.

---

## 7. Constraints

- One backend touch permitted: the `sector` field (§3). Nothing else in
  `backend/`.
- `src/config.py` may gain `sector` per asset; do not change existing
  names, IPs, criticality, purdue levels, or edges (the additive rule that
  kept 518 tests green).
- No raw hex/`rgba()` in components — tokens only.
- Real data only; the `:8001` mock stays down.
- Commit nothing until the suite is green.
