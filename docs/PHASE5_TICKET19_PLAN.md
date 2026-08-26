# Ticket #19 Plan — styling pass, states, deferred fixes

Planning authority: this document. `docs/DESIGN_CONSOLE.md` governs visuals.

These are **measured defects**, not a vague "polish pass". Fix these; do
not open new design questions.

---

## A. Telemetry feed is 100% clipped, and lies about precision (HIGH)

Measured live at 1440×860 with a real replay:

```
rail width              : 280px
rows rendered           : 200
visually clipped rows   : 200   (every single one)
distinct milliseconds   : {"000"}   across all 200 rows
distinct seconds        : {"00"}    across all 200 rows
```

Two problems, one fix:

1. **Unreadable.** The destination is cut off on every row —
   `14:38:00.000 192.168.10.9 → 210.69…`. The DOM text is complete, so
   this is CSS clipping, introduced when D-R1 narrowed the rail 340→280px
   to give the graph its width.
2. **False precision.** `.000` is *always* `.000` and `:00` is *always*
   `:00`, because this capture day is minute-bucketed. Rendering
   millisecond precision the data does not have is exactly the kind of
   overstatement this project rejects elsewhere.

**Fix:** drop the `.mmm` component — it is never meaningful for this
dataset and frees four characters. Keep `HH:MM:SS`: seconds *are*
meaningful on capture-seconds days, and `timing_provenance` on each event
already distinguishes `capture_seconds` from
`interpolated_minute_bucket`. Do **not** hardcode dropping seconds.

Then make the row actually fit at 280px. Reasonable options, combinable:
shorten the arrow gutter, allow the source/destination to use the freed
space, or drop the leading `192.168.` octets **only** when both endpoints
share a prefix (and say so in a column header or tooltip if you do).
A row must not claim more precision or less identity than it has.

Acceptance: **0 visually clipped rows** at 280px, measured the same way.

---

## B. Graph camera leaves the cluster layer out of frame (deferred from #14)

In expanded mode the camera frames the curated layer, so the `/24` cluster
nodes — the second half of the two-layer K8 story — sit outside the
viewport. The header still reports them (`24 /24 clusters`), so the UI
says one thing and shows another.

**Fix:** the default framing must include both layers. If both cannot fit
legibly at once, the honest fallback is a visible affordance (a "fit all"
control, or an edge indicator showing off-screen clusters) rather than
silently cropping half the model.

---

## C. Telemetry rail loses internal scroll below `xl` (deferred from #11)

Below the `xl` breakpoint the rail has no definite ancestor height, so all
200 buffered rows render inline and the page grows very long. Nothing is
clipped or unreachable, so this is a comfort defect, not a correctness
one — give the rail a bounded height with its own scroll at stacked
widths.

---

## D. Sector-focus overlap

With a sector expanded (e.g. Finance), its member assets are drawn over
the hub and neighbouring sectors, and their labels collide
(`City_Payment_Gateway` / `Social_Welfare` / `Municipal_Bond` /
`Tax_Collection` overlapping). Expanded members must be placed in their
own sector's wedge with the same collision-avoidance the top-level layout
already uses.

---

## E. States and reconnect audit (the ticket's original scope)

Walk every panel and confirm: loading, empty, and error states exist, are
reachable, and say something literally true. Specifically re-verify the
WebSocket reconnect path end to end (kill backend → both API and stream
indicators degrade → restart → both recover with no page reload), since
this project has shipped a false "retrying" message before.

`RISK` shows `—` only when there is no basis to compute it; `0` is a real
value and must render as `0`.

---

## Do not regress

Two-layer separation and its honest caption; the 24-cluster cap; pinned
curated layout and readable labels; cascade animation driven by the real
`impacted` payload with median **and** p5–p95; sector view + ⤢ expand;
sector health strip; replay progress; alert severity counts and
`alerts_suppressed`; one WebSocket per tab; never-a-blank-panel.

---

## Verification

```bash
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q   # 538 baseline
./venv/bin/ruff check src/ backend/ --select E,F,W --ignore E501
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

Live at 1440×860, replay at speed 20:
1. Re-run the §A measurement and report `visuallyClippedRows` — must be 0.
2. Screenshot showing both graph layers in frame.
3. Expand a sector; screenshot; confirm no label collisions.
4. Widths 1440 / 1280 / 1000 / 860: nothing clipped or unreachable, rail
   scrolls internally where stacked.
5. Kill/restart the backend; confirm both indicators degrade and recover
   with no reload.
6. Zero console errors in a fresh tab.

---

## Constraints

- `src/` must not change.
- No raw hex/`rgba()` in components — tokens only.
- Real data only; the `:8001` mock stays down.
- Commit nothing.
