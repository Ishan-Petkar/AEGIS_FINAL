# Ticket #14 Plan — CII cascade animation + graph legibility

Planning authority: this document. `docs/DESIGN_CONSOLE.md` governs visuals.

---

## 1. Scope

Two things, deliberately in one ticket: the cascade animation is the
demo's visual payoff, and it is pointless on a graph nobody can read.
Ticket #11 recorded the legibility problem and deferred it to #19; it is
pulled forward here because #14 depends on it.

**In scope:**
1. **Graph legibility** — make the two layers readable at 16 curated + up
   to 24 cluster nodes.
2. **Cascade animation** — on a `cii` envelope, animate the blast radius
   outward from the origin asset across the curated dependency edges.

**OUT of scope:** alerts ack (#15), `/api/stats` (#16), README/SDG
(#17/#18).

---

## 2. Decision: anchor the curated layer, float the clusters (D14-1)

Ticket #11's force simulation lays out *both* layers, so 24 arriving
clusters shove the 16 curated nodes into a cramped knot and the two-layer
distinction — the entire point of the K8 design — is visually lost.

The curated topology is **16 nodes with a known, fixed structure that
never changes at runtime**. There is no reason to re-derive its layout
from physics every frame.

- **Curated layer:** compute a stable layout once (force-settle it once at
  mount, or lay it out deterministically by Purdue level), then **pin the
  nodes** (`fx`/`fy`). The city model should sit still — an operator
  learns its shape and expects it to stay put between glances.
- **Cluster layer:** stays force-driven, confined to its own region, and
  must not perturb the pinned curated nodes.
- The layers must remain visually separable at a glance (§DESIGN §6:
  clusters hollow/dashed/muted; curated solid).
- Node labels must not overlap illegibly at the default zoom. If labels
  collide, show them for curated nodes and on hover for clusters.

A stable curated layout also makes the cascade animation legible — an
animation across nodes that are themselves drifting is unreadable.

---

## 3. Decision: animate real CII output, never a scripted path (D14-2)

The `cii` envelope (`backend/ingest.py`) carries:
`{snapshot_id, origin_asset, cii_median, cii_p5, cii_p95, impacted,
trigger_event_id}` where `impacted` is the **real ordered list** of
impacted assets from the Monte Carlo run (ordered by compromise
frequency).

- Animate strictly from that payload. **Do not hardcode a cascade path**
  for the demo — a scripted animation that does not reflect the actual
  computed blast radius would be exactly the fabricated-signal problem
  this project has refused throughout (P5-15, D8-3, D11-1).
- Origin node pulses `--sev-critical`; impacted nodes light in sequence
  along the curated edges, staggered so the propagation reads as
  directional rather than everything flashing at once.
- Show the actual numbers alongside it — median with the p5–p95 interval,
  because CII is reported as a **distribution, not a point estimate**
  (this is a core project claim; do not render only the median).
- The animation must be **interruptible and non-blocking**: a second `cii`
  envelope arriving mid-animation replaces the first cleanly. At high
  replay speeds these can arrive frequently — never queue an unbounded
  backlog of animations.
- Respect `prefers-reduced-motion`: show the final highlighted state and
  the numbers, with no motion.

---

## 4. Requirements

- Consume the shared `useStream()` context — **never** call
  `useEventStream()` directly (that reintroduces the duplicate-socket
  defect fixed in #10).
- Do not regress: two-layer separation, the 24-cluster cap, the honest
  caption, one socket per tab, the four-width responsive behaviour, the
  no-runaway-canvas property, or the "never a blank panel" rule.
- No raw hex/`rgba()` in components — tokens only.
- Real data only. Mock stays down.

---

## 5. Verification

```bash
cd frontend && npx tsc --noEmit && npm run lint && npm run build
git status --short src/ backend/     # empty
```

In the browser, against the real stream:
1. Start a real replay; confirm the curated layer is **legible and
   stationary** while clusters arrive, and the two layers stay visually
   distinct. Report the rendered node count.
2. `POST /api/inject {"scenario":"honeytoken","target_asset":"City_Payment_Gateway"}`
   — confirm a cascade animates from `City_Payment_Gateway` across the
   **real** impacted assets, and that the displayed impacted set matches
   the `cii` envelope's `impacted` payload exactly (cite both).
3. Confirm median + p5–p95 are shown, not just the median.
4. Fire two injects in quick succession — confirm the second animation
   replaces the first cleanly with no backlog.
5. `prefers-reduced-motion` → final state shown, no motion.
6. Zero console errors in a fresh tab. Screenshot mid-cascade.

---

## 6. Constraints

- Do not modify `src/` or `backend/`.
- Commit nothing.
