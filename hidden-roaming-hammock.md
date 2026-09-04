# AEGIS Console — Restore, Re-graph, Re-classify

## Context

The light-theme redesign I just shipped changed the token layer and restructured `page.tsx`. It also **silently regressed real functionality**, which is what you noticed as "things missing":

- **The graph's live behaviour never broke — but it became invisible and unreachable.** `page.tsx:28`'s row wrapper has no definite height, so `GraphPanel`'s `xl:h-auto` has nothing to stretch against and the canvas is now sized by the *alerts list* beside it. `AlertsRail`'s internal scroll stopped engaging for the same reason (100 cards grow unbounded).
- **The cascade/attack-path highlight has a pre-existing bug that the aggregated default view exposes**: `pathEdgeIds` holds *real* asset ids (`curated:A->B`) while the collapsed view's links are keyed by *display* ids (`curated:sector:finance->sector:core`), so **path edges never light up unless both endpoints happen to be expanded**. Lit nodes, no lit path — exactly what you're missing.
- **`DetectionPreventionPanel` lost 10 affordances** the deleted `IpsActionsRail` had — most importantly it is **no longer live** (it's REST-only; `useEventStream`'s `ipsActions` is now dead code), and it dropped `reason`, `evidence`, the TTL countdown, rollback details, and per-row error feedback.
- **Light-theme casualties**: the graph's label backing plate fills white-on-white; `TelemetryRail` fades rows to 35% opacity (a dark-theme depth trick that deletes the feed on white); `theme-tokens.ts` `FALLBACK` is still the *dark* palette.

On top of the repairs you want two new capabilities: **per-asset icons on graph nodes** and a **TYPE column** in the telemetry log, plus a **linear incident-path view** matching your reference image.

Decisions taken: TYPE column is **detection-derived + signature rule names** (not the dataset answer key); graph gets **icons + cascade work AND** a linear incident-path strip.

---

## Phase 0 — Repair the regressions

**0.1 Layout height (the big one).** `frontend/src/app/page.tsx:28` — the `<div className="flex flex-col gap-3 lg:flex-row">` wrapping `GraphPanel` + `AlertsRail` has no height. Give it a definite height at `xl` (e.g. `xl:h-[56vh] xl:min-h-[460px]`). This single change fixes both the AlertsRail non-scroll and the GraphPanel canvas blow-up. Because the right column is now `xl:overflow-y-auto` (a scrolling region, not a viewport-fitted grid), a definite height is the correct model — viewport-fitting is no longer available to this panel. Verify `AlertsRail.tsx:489`'s `overflow-y-auto` engages again.

**0.2 Light-theme legibility.**
| Site | Fix |
|---|---|
| `TelemetryRail.tsx:256` `Math.max(0.35, 1 - i*0.04)` | Raise floor to ~0.75 or drop the fade — 0.35 on white ≈ 1.5:1 |
| `AlertsRail.tsx:276` `opacity-55` | Replace with an explicit muted surface (`bg-glass-raised` + `text-text-dim`) |
| `border-glass-border/50` `/60` (`TelemetryRail.tsx:268`, `DetectionPreventionPanel.tsx:168,266`) | Full-strength `border-glass-border` — the token is now an opaque light hex, so alpha cuts erase it |
| `--text-mute` `#98a2b3` on ≤11px text (~15 sites) | Darken token to ~`#667085` in `globals.css` for AA |
| `MetricsStrip.tsx:86` `text: "bg-glass-raised …"` | Invisible medallion (`#f7f8fa` on `#ffffff`) — use a bordered or tinted variant |

**0.3 `frontend/src/lib/theme-tokens.ts:42-57`** — `FALLBACK` is still `ground:"black"`, `accent:"orange"`. Flip to light CSS keywords (`whitesmoke`/`white`/`black`/`royalblue`/…). Per-token fallback means one missing CSS var currently yields an orange node on a white canvas.

**0.4 `DetectionPreventionPanel` parity.** Restore what `IpsActionsRail` had:
- **Subscribe to `useStream().ipsActions`** and merge with REST by id (newest lifecycle state wins) — the panel is currently blind to new decisions, TTL expiries, and other operators' rollbacks until a reconnect.
- Expandable row detail showing `reason` prose + `evidence` (threat_score, band, fired detectors, criticality, CII median) + `triggering_event_id`.
- TTL countdown from `expires_at`; `rolled_back_at` + `rollback_reason` on terminal rows.
- Per-row rollback **error feedback** — `DetectionPreventionPanel.tsx:118-122` currently swallows 404/401/network in a bare `catch {}`. Mirror `AlertsRail`'s `AckMeta` pattern.
- Pass `active` to `getIpsActions` (already supported at `api.ts:199-211`) so `activeCount` isn't under-counted past the 100-row page.

**0.5 Stale prose.** `InjectControl.tsx:156-173` and `GraphPanel.tsx:60-67` describe a `--glass` "4.5% alpha fill" and a `backdrop-filter` rule that `globals.css` no longer contains. `docs/FEATURES.md:255-260` and `AEGIS_Judge_Room_Dossier.md:399` promise a TTL countdown the UI currently lacks (0.4 makes them true again).

---

## Phase 1 — Graph legibility foundation *(independent, land first)*

All in `CityGraph.tsx` unless noted. Do these **before** markers — with edges recessive, every marker problem becomes visible.

1. **Edges recede** (`linkColor` L1637-1647, `linkWidth` L1649-1659): default curated `colors.textDim` → `colors.glassBorderStrong`, width 1.4 → 1.0; gateway edges keep `accent` at 1.2; cascade red stays 2.6. 63 dark-slate edges currently outweigh the nodes they connect. Also width-scale aggregated edges by their existing `count`.
2. **Label backing plate** (L1576-1586): fills `--ground-raised` = `#ffffff` on a white canvas — invisible, and its `--glass-border` stroke is ~1.2:1. Fill `--ground` (`#f4f5f7`, the one token guaranteed to differ from the panel behind it) at alpha **1.0**, stroke `--glass-border-strong`, and divide `lineWidth`/corner radius by `globalScale` (text is held at constant screen size at L1571, so the border grew at zoom).
3. **Alpha sweep** — every `globalAlpha` was tuned for glow-on-black; on white low alpha reads as unfinished, not dim. Raise the pulse floor (L1475/L1613 `0.35` → `0.55`), origin ring (L1495), impacted ring (L1517 → 1.0), cluster dash (L1603 → 1.0). Fix cluster hover label L1629 `textMute` → `textDim` (2.3:1 → 8.6:1). **Rule: fills may fade, strokes/glyphs/labels never do.**
4. **Cascade clears** — nothing ever sets `cascade` back to `null` (`setCascade` only at L1326), so rings persist all session and a live cascade looks identical to an hour-old one. Add a frontend-owned TTL (`revealMs + ~9s hold`) with a `startedAt` identity guard so a newer envelope's timer can't clear it.

---

## Phase 2 — Node sizing *(coupled; land together)*

Icons need room: `curatedNodeRadius` (L256-258) currently yields **3–10px**.

- `curatedNodeRadius` → `11 + criticality * 7` (12–18px for real values). Compress the dynamic range deliberately — radius was never a precise criticality readout; severity/cascade rings carry that.
- Add a single `curatedMarkerRadius({id, criticality, isAggregate, isGateway})` so paint, layout, `nodeVal` and hit-testing agree **by construction**. Hub `max(base*1.45, 24)`; aggregate `base*1.2`; gateway floor 13.
- Point `computeCuratedLayout` L633 at the same function (today the layout thinks the hub is 10px while paint draws 16), which auto-pushes labels outward.
- Raise `coreFrac` L505 `0.15 → 0.22` (and `minSectorFrac` to `coreFrac*1.55`) — otherwise the enlarged hub halo collides with the 5-node core ring.
- **Fix hit-testing**: `nodeVal = (markerRadius / NODE_REL_SIZE)²` with explicit `nodeRelSize={4}`, plus a `nodePointerAreaPaint` circle at `markerR + 4`. Today *every* node is a ~4px click target regardless of drawn size — and sector-aggregate click-to-expand is the primary interaction. `nodeVal` is `triggerUpdate:false` and read by no d3 force, so this is physics-safe; it also fixes link endpoints and `zoomToFit` framing.

---

## Phase 3 — Node icons *(depends on Phase 2)*

New `frontend/src/lib/asset-icons.ts`: hand-authored 24×24 **stroke-only** SVG path strings (no new dependency — `package.json` has 4 runtime deps and an icon package ships 1000+ glyphs for the 15 used). Exports `ICON_PATHS`, `iconKeyFor(node)`, lazily-cached `iconPath2D(key)`, and `drawIcon(ctx, …)`.

**Critical**: `Path2D` objects must be module-scope cached — `nodeCanvasObject` runs for every node every rAF (`autoPauseRedraw={false}`, L1774).

15 glyphs mapped from the 16 real `type` values in `src/config.py` plus the `isGateway`/`isAggregate`/`observed` flags: `sensor`, `database`, `plant`, `controller`, `shield`, `bank`, `globe`, `mesh`, `router`, `building`, `cross`, `ballot`, `exchange`, `gateway`, `layers`, `subnet`.

**Marker restructure** — resolve `role` → `hue` → `iconKey` **once** at the top of the curated branch, then paint three co-centric layers in one hue: soft tint wash (α ~0.12) → full-strength hairline ring → glyph. This replaces today's five independent colour choices and is what lets an *impacted* node re-hue to `sevWarning` while keeping its own shape and glyph (today it stays accent-blue wearing an orange halo).

Also: the hub's `colors.ground` "dark core" (L1417-1421) is dead on a light theme — delete it; the hub instead becomes the one marker with a **dark `colors.text` glyph** (~15:1, the highest-contrast mark on the canvas). Fix `isFinancial` (L324) — `.includes("Financial")` misses `Social_Welfare_System` ("Social Safety Net"); use an explicit `FINANCIAL_TYPES` set. Rewrite the `Legend` (L1817+) to render the *same* `ICON_PATHS` as inline `<svg>` so legend and canvas cannot disagree. Add `ICON_MIN_SCALE ≈ 0.55` LOD so glyphs degrade to plain discs when zoomed out.

---

## Phase 4 — Cascade correctness + motion *(depends on Phase 3)*

1. **Extract `makeDisplayRemap`** from `buildDisplayTopology`'s inline `remap` (L400-404) — two callers now need the identical rule.
2. **`displayCascade` memo** projecting the real cascade onto current display ids. **This is the fix for the missing attack path.** Also projects `hopOf` (fixing aggregates always revealing last via `fallbackHop`) and derives `flowForward` from the BFS parent chain. Keep `computeCascadeGeometry` pure over the real topology — the route is a fact about the city, not about what's expanded. Leave the L1359 auto-focus alone; once this lands the collapsed view lights correctly with nothing expanded.
3. **`revealedHop` state** (≤6 timers per cascade) replacing the two per-frame `performance.now()` reads in `linkColor`/`linkWidth`. Required because particle accessors are *not* re-evaluated per frame — the reveal must be pushed, not polled.
4. **Animated particles along the lit path**: `linkDirectionalParticles/Speed/Width/Color`. Verified safe — the prop is `triggerUpdate:false` with `onChange: updDataPhotons`, and react-kapsule re-applies on identity change, so this rebuilds `link.__photons` **without** touching `graphData` and **without** a simulation reheat. The no-`setGraphData` discipline (L1183-1198) is preserved. Use **signed speed** from `flowForward` so motion always points *away from the origin* — a display edge's `source→target` is a dependency direction, not the direction compromise travelled. (Same reason: do **not** enable `linkDirectionalArrowLength`.)
5. **Origin reads as compromised**: red hexagon + red `!` badge, drawn as a hard override *before* the hub/gateway/financial branches, with the node's own glyph still stroked inside. Shape survives what colour alone doesn't (CVD, greyscale screenshots, the LOD floor).

---

## Phase 5 — Linear incident-path view (new)

Extract the cascade BFS into `frontend/src/lib/cascade.ts` (shared by `CityGraph` and the new component — one implementation, no drift). New `frontend/src/components/IncidentPathStrip.tsx`, rendered between the graph row and the detection panel in `page.tsx`.

Renders the **real** chain from the live `cii` envelope, left to right, like your reference: `External Network → Gateway (Purdue zone) → [compromised origin, red hexagon + !] → hop-1 assets → hop-2 assets`, each as an icon chip reusing `asset-icons.ts`, connected by arrows, with hop-staggered reveal matching the graph's `CASCADE_STAGGER_MS`. Node colouring: origin red, impacted amber, untouched grey. Honest empty state when no cascade is live ("No active incident — the path appears when a compromise is detected").

The gateway hop is real, not decorative: the topology's Purdue-zone gateways are genuinely on the BFS route (that's the mandatory-access-gateway model), so the chain shows what the cascade actually traversed.

---

## Phase 6 — Telemetry TYPE column

**Backend (one additive field).** In `backend/ingest.py:1997-2007`, add `matched_rules` to the `hybrid` envelope dict — a flat list of fired signature **rule ids** pulled from the verdict evidence that already exists (`backend/detection/signature.py:434-447`). Ship ids, not titles: the envelope is broadcast for every event at ~2000/s and `signature.py:250-253` guarantees `rule_id` stability. `DETECTOR_SIGNATURE` is already imported in `ingest.py`.

**Test to update**: `tests/test_ingest_hybrid.py:305` is an exact-key-set assertion on the hybrid envelope — extend it. (`tests/test_websocket.py:406` asserts REST/WS key parity; `useEventStream.ts`'s `hybrid: null` on backfilled rows keeps that honest.) Update the `_broadcast_batch` docstring, which enumerates the key list verbatim.

**Frontend.** Extend `EventEnvelopeData.hybrid` in `types.ts`, then add a TYPE column to `TelemetryRail.tsx` resolving in precedence order:

| Tag | Source | Tone |
|---|---|---|
| `TRIPWIRE` | `tripwire_fired` | critical |
| `KNOWN-BAD` / `C2-SHAPED` / `ADMIN-PORT` / `DB-EXPOSED` | `hybrid.matched_rules` → `AEGIS-SIG-001/002/004/005` | critical / warning |
| `BEACON` | `hybrid.fired_detectors` includes `beaconing` | warning |
| `KNOWN-THREAT` | includes `random_forest` | warning |
| `ANOMALY` | `is_anomaly` | warning |
| `NORMAL` | none of the above | normal |

Reuse `TelemetryRail.tsx:284-294`'s existing `inject` pill markup and its tooltip-explains-provenance pattern. Rows stay two-line; TYPE sits on the first line opposite the timestamp. Add the reference's "All Traffic" filter as a select over these tags. Backfilled REST rows have `hybrid === null` — they fall back to `TRIPWIRE`/`ANOMALY`/`NORMAL` rather than showing a fabricated tag.

**Do not** surface `Event.raw.label` (the CIC-IDS2017 answer key). It exists on the REST path but is absent from every live row, and placing a dataset label beside detector output in the same row is exactly the confusion `batch_origin` and `timing_provenance` were added to prevent.

---

## Phase 7 — Docs

Update `docs/FEATURES.md` §5 (TTL countdown, the new panels), `docs/DESIGN_CONSOLE.md` (already marked superseded — add the icon/marker system), and the graph section of `AEGIS_Judge_Room_Dossier.md` + its markdown twin. Note the dossier artifact is live at the existing URL and should be republished.

---

## Verification

Run the backend + console (`preview_start` on `aegis-backend` / `aegis-console`), start a replay, and inject a scenario.

**Per phase:**
- **0.1** — at ≥1280px, alerts panel scrolls internally; graph canvas holds a stable height (check `CityGraph`'s ResizeObserver isn't re-firing) and does not track the alerts list's length.
- **0.4** — fire an injection with `ips_enabled=true`; a new prevention row must appear **without a page reload**. Click Roll back with the backend stopped → a visible error, not a silent no-op.
- **1** — watch one full pulse cycle on a warning node: never disappears. Hover a periphery node whose label crosses an edge: plate fully occludes it. Fire two CII envelopes ~15s apart: the first clears before the second lands.
- **2** — expanded view at max width: no marker overlap, no gateway touching the hub halo. Click a sector bubble 10×: 10 expansions, 0 misses.
- **3** — all 16 types resolve to distinct glyphs; zoom out past the LOD floor → clean fall back to discs; legend swatches match the canvas exactly.
- **4** — **the headline test**: fire a cascade whose origin is in Finance with impact spanning Energy, **in the default collapsed view**. Red path edges must light *between aggregate bubbles*, particles must flow away from the origin, origin must be a red hexagon + badge, impacted aggregates must reveal in hop order.
- **5** — incident strip shows the same origin/hops as the graph (they share `lib/cascade.ts`, so a mismatch is a bug).
- **6** — `PYTHONPATH=src venv/bin/python -m pytest tests/test_ingest_hybrid.py tests/test_websocket.py -q`; then confirm live rows show rule-derived tags and backfilled rows degrade gracefully.

**Whole-suite gates:** `ruff check src/ backend/ --select E,F,W --ignore E501`, `AEGIS_TEST_LIVE_DB=1 pytest tests/ -q` (currently 735 passed / 0 skipped), and in `frontend/`: `npx tsc --noEmit` + `npx eslint src` (both currently clean). **There is no frontend test suite** — every frontend change above needs manual browser verification; nothing will catch a regression automatically.
