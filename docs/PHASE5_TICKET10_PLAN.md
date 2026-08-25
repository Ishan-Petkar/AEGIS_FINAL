# Ticket #10 Plan — Live telemetry feed

Planning authority: this document. `docs/DESIGN_CONSOLE.md` governs visuals.

---

## 1. Scope

Replace `TelemetryRail`'s static placeholder rows with the real stream from
`useEventStream()` (Ticket #4). That hook already does the transport,
reconnection, and buffering — **do not reimplement any of it.** It exposes
`events` (newest first, capped at 200), `status`, `eventsPerSecond`.

**In scope:** row rendering, ordering, readability under load, freeze-on-
interaction, empty/connecting/disconnected states, render throttling.

**Data source: the real stream only.** Ticket #9 made
`WS :8000/ws/stream` the default and it carries real replayed
CIC-IDS2017 traffic. The Ticket #4 mock is a reconnect-test fixture and
must never be the source of rendered data.

**OUT of scope:** the graph (#11), ack wiring (#15), `/api/stats` (#16).
Leave `AlertsRail` alone. #9 and #12 are already done.

---

## 2. Decision: newest at top, no scroll-chasing (D10-1)

`PHASE5_BUILD_PLAN.md` §8 says "autoscroll". Implement that as **newest
row at the top of a top-pinned list**, not as a scroll-to-bottom animation.

Reasoning: at a real 20× replay's event rate — and far worse at the higher
speeds the operator can dial in — a scroll-to-bottom feed is unreadable and
fights the user the moment they try to look at anything. Newest-at-top gives the same
"latest is immediately visible" property with no scroll mechanics, no
jank, and no "did the user scroll up?" state machine. `useEventStream`
already returns newest-first, so this is also the natural order.

Age fade runs **downward** (freshest at full `--text`, oldest toward
`--text-mute`), per `DESIGN_CONSOLE.md` §6.

---

## 3. Decision: freeze on interaction (D10-2)

**This is the requirement that makes the feed usable at all.** At 8+
rows/sec a human cannot read a row before it moves. So:

- On hover over the feed, or any scroll within it, **freeze** the rendered
  list — hold the current rows and stop accepting new ones into the view.
- Show an explicit, honest indicator while frozen (e.g. a small `PAUSED ·
  N new` chip) — the copy must state what is actually happening, and the
  count must be real, not decorative.
- Resume on mouse-leave, or via a click/keypress on the indicator.
- Freezing affects **display only**. The underlying hook keeps receiving,
  and the header's `events/s` keeps ticking — the stream is not paused,
  the view is. Do not let the indicator imply otherwise.

---

## 4. Decision: throttle rendering, not receiving (D10-3)

A gentle rate is survivable by any implementation. **The real stream is
already the default** (Ticket #9), and a 20×+ replay can push hundreds of
events per second — a re-render per message will lock the tab.

Therefore: batch view updates on a fixed cadence (~100ms, or one
animation frame), rendering the latest snapshot rather than every message.
The hook keeps ingesting at full rate; only the DOM update is throttled.

Verify this deliberately, and **against the real stream, not the mock**
(no synthetic data anywhere): start a real replay at high speed, e.g.
`POST /api/replay/start {"dataset":"friday-morning","speed":500}`, and
confirm the UI stays responsive and the 200-row cap holds. A feed that
only works at 8/sec is not done — it is a feed that has not met its
actual load yet. Ticket #9 measured the real path sustaining a live
replay at `consumer_error_count: 0` and 0.012s lag, so any jank observed
here is the renderer's fault, not the transport's.

---

## 5. Row content

Columns, mono, `tabular-nums` on the timestamp:

```
HH:MM:SS.mmm   <source> → <destination>            <glyph>
```

- **Identity:** prefer `source_asset`/`destination_asset`; fall back to
  `source_ip`/`destination_ip` when the asset is an `Unresolved_*` name.
  Per K8, most real replay traffic resolves to `Unresolved_<ip>`, so
  showing that string verbatim would fill the feed with noise — show the
  IP instead, which is the more informative of the two.
- **Glyph + severity:** `tripwire_fired` → critical (`■`,
  `--sev-critical`); `is_anomaly` → warning (`▲`, `--sev-warning`);
  otherwise normal (`●`, `--sev-normal`). Reuse the existing
  `SeverityGlyph`. Anomalous rows also take a 2px left border in their
  severity color (`DESIGN_CONSOLE.md` §6).
- Severity must never be conveyed by color alone — the glyph carries it
  too (§7 accessibility floor).
- Truncate long names with ellipsis; the row must never wrap or cause
  horizontal scroll.

---

## 6. States

- **connecting** — "connecting to stream…"
- **connected + zero events** — "connected — waiting for events" (this is
  a real state: the backend may be up with no replay running)
- **reconnecting / disconnected** — say so plainly; copy must be true (a
  false "retrying" was a real defect in Ticket #3)
- Never render a blank panel.

---

## 7. Verification

```bash
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

In a browser, against the **real** backend stream (`WS :8000/ws/stream`,
Ticket #9) with a real replay running — the mock must not be used as a
data source:
1. Rows appear, newest at top, timestamps advancing.
2. Anomalous rows show the correct glyph and left border.
3. Hover the feed → freezes, indicator shows a real pending count;
   mouse-leave → resumes and catches up.
4. **High-rate test:** run a real replay at `speed=500` (or higher) and
   confirm the page stays responsive and the row cap holds at 200.
5. Stop the backend → feed shows disconnected, and recovers when it
   returns (Ticket #9's reconnect path).
6. Zero console errors (check in a fresh tab — the message buffer
   accumulates across navigations).
7. Screenshot.

Backend untouched: `git status --short src/ backend/` empty;
`PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q` still 494 passed.

---

## 8. Constraints

- Do not modify `src/` or `backend/`, or `useEventStream.ts`'s transport
  logic. If the hook genuinely needs a new field, add it additively and
  say so.
- No raw hex or `rgba()` in components — tokens only.
- Commit nothing.
