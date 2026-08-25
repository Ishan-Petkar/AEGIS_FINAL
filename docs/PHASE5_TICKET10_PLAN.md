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

**OUT of scope:** the graph (#11), ack wiring (#15), real WS endpoint (#9),
mock→real swap (#12), `/api/stats` (#16). Leave `AlertsRail` alone.

---

## 2. Decision: newest at top, no scroll-chasing (D10-1)

`PHASE5_BUILD_PLAN.md` §8 says "autoscroll". Implement that as **newest
row at the top of a top-pinned list**, not as a scroll-to-bottom animation.

Reasoning: at the mock's 8 events/sec — and far worse once #12 connects a
real replay at 20×+ — a scroll-to-bottom feed is unreadable and fights the
user the moment they try to look at anything. Newest-at-top gives the same
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

The mock emits ~8/sec, which any implementation survives. **Ticket #12
will point this at the real stream**, where a 20×+ replay can push
hundreds of events per second, and a re-render per message will lock the
tab.

Therefore: batch view updates on a fixed cadence (~100ms, or one
animation frame), rendering the latest snapshot rather than every message.
The hook keeps ingesting at full rate; only the DOM update is throttled.

Verify this deliberately: run the mock at a high rate (`--rate 300` or
similar) and confirm the UI stays responsive. A feed that only works at
8/sec is not done — it is a feed that has not met its actual load yet.

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

In a browser, with the mock running:
1. Rows appear, newest at top, timestamps advancing.
2. Anomalous rows show the correct glyph and left border.
3. Hover the feed → freezes, indicator shows a real pending count;
   mouse-leave → resumes and catches up.
4. **High-rate test:** restart the mock at a much higher rate and confirm
   the page stays responsive and the row cap holds at 200.
5. Kill the mock → feed shows disconnected, header API stays connected.
6. Zero console errors (check in a fresh tab — the message buffer
   accumulates across navigations).
7. Screenshot.

Backend untouched: `git status --short src/ backend/` empty;
`PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q` still 483 passed.

---

## 8. Constraints

- Do not modify `src/` or `backend/`, or `useEventStream.ts`'s transport
  logic. If the hook genuinely needs a new field, add it additively and
  say so.
- No raw hex or `rgba()` in components — tokens only.
- Commit nothing.
