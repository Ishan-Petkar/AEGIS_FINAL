# DESIGN_CONSOLE.md — AEGIS Operations Console (Next.js)

**Scope.** This document governs the **Phase 5 Operations Console** only —
the Next.js surface at `frontend/`. It does **not** govern the Streamlit
Research Console (`src/aegis_demo.py`), whose tokens live in
`docs/DESIGN.md` and whose CSS already implements them. The two consoles
are deliberately different surfaces (PLAN_MASTER Decision #9); do not
"unify" them by editing one to match the other.

Where `docs/PHASE5_BUILD_PLAN.md` §3 and `PLAN_MASTER.md` §Phase-5 say the
scaffold ports `docs/DESIGN.md` tokens, **this file supersedes that** for
the Operations Console.

---

## 1. Direction: Warm Industrial Glass

Two ideas fused deliberately:

- **Industrial instrument** — warm charcoal grounds, amber-gold as the
  brand accent, mono-forward numerics, dense data. The reference is
  backlit control-room instrumentation bolted to a substation wall, not a
  SOC screenshot.
- **Depth / glass** — panels are translucent, blurred, layered over an
  ambient ground glow, with softened geometry.

**Why this pairing.** Glass UI in the wild is nearly always cool-toned
(periwinkle/cyan over blue-black). Security dashboards are nearly always
blue-cyan. Warm amber instrumentation seen *through* smoked glass is
neither, which is the point: this is a cyber-**physical** infrastructure
product, and the palette should say "municipal plant" before it says
"hacker console."

**Anti-goal.** Do not drift toward the CrowdStrike/Splunk electric-cyan
look this replaces, and do not drift toward generic SaaS glassmorphism
(no purple-blue gradients, no floating cards on a blurred photo).

---

## 2. Color tokens

All colors are defined once as CSS custom properties on `:root` and
consumed through Tailwind theme names. **Never hardcode a hex in a
component.**

### Ground and material

| Token | Value | Use |
|---|---|---|
| `--ground` | `#14120e` | page background, warm near-black |
| `--ground-raised` | `#1c1917` | opaque fallback surface |
| `--glow-warm` | `rgba(232,163,61,0.07)` | ambient radial glow behind the layout |
| `--glass` | `rgba(255,247,235,0.045)` | standard panel fill |
| `--glass-raised` | `rgba(255,247,235,0.075)` | elevated panel / hover fill |
| `--glass-border` | `rgba(255,240,220,0.10)` | panel hairline |
| `--glass-border-strong` | `rgba(255,240,220,0.18)` | active/focused panel |

Panels use `background: var(--glass)` + `backdrop-filter: blur(14px)
saturate(120%)`. Always pair a glass fill with an opaque fallback
(`--ground-raised`) via `@supports not (backdrop-filter: blur(1px))` —
blur is a progressive enhancement, never a legibility dependency.

### Text

| Token | Value | Use |
|---|---|---|
| `--text` | `#f2ede4` | primary |
| `--text-dim` | `#a89f90` | secondary / labels |
| `--text-mute` | `#6f665a` | tertiary, timestamps, disabled |

### Brand vs. severity — keep these separate

`--accent` is the **brand/interactive** color. Severity colors are a
**separate scale**. This distinction is load-bearing: if amber means both
"interactive" and "warning," an operator cannot read severity at a glance.

| Token | Value | Meaning |
|---|---|---|
| `--accent` | `#e8a33d` | brand, primary action, live indicator, focus ring |
| `--accent-hi` | `#f2b658` | accent hover |
| `--sev-critical` | `#e5484d` | critical — used rarely, so it means something |
| `--sev-warning` | `#f5c542` | warning — deliberately **yellower** than `--accent` so the two never read the same |
| `--sev-normal` | `#7cb342` | healthy / pass |
| `--sev-info` | `#7d9cc0` | informational, muted cool tone |
| `--financial` | `#d4a843` | financial-system nodes **only**, never elsewhere |

**Rule:** `--accent` never encodes state, and severity colors never
appear on a button, link, or focus ring. Verify at review time.

---

## 3. Typography

| Role | Face | Notes |
|---|---|---|
| UI / body | **Inter** | 400/500/600/700 |
| Numerics, IPs, timestamps, IDs | **JetBrains Mono** | 400/600, `font-variant-numeric: tabular-nums` **always** |

Chosen for density, not novelty — this screen is read, not admired. The
character comes from palette and material, not a quirky display face.

- H1 `28px/700`, H2 `20px/600`, H3 `16px/600`; headings `letter-spacing: -0.02em`
- Body `14px/1.5`
- Labels/captions `11px`, uppercase, `letter-spacing: 0.08em`, `--text-dim`
- Hero metrics `JetBrains Mono 600`, `24–32px`, with a faint text-shadow
  in the metric's own semantic color

Load both from Google Fonts (the only font host the CSP admits) with real
fallback stacks: `Inter, system-ui, -apple-system, sans-serif` and
`"JetBrains Mono", ui-monospace, "SF Mono", monospace`.

---

## 4. Geometry, spacing, motion

- **Radii:** `4px` on panels and buttons, `2px` on dense table rows and
  badges. The midpoint between instrument-sharp and glass-soft — softened
  enough to read as glass, tight enough to read as equipment.
- **Spacing:** strict 4px baseline → `4, 8, 12, 16, 24, 32, 48`. Panel
  padding `16px`, grid gap `12px`. No arbitrary values.
- **Layout spacing** comes from flex/grid `gap`, never per-child margins.
- **Borders:** 1px hairlines only. Depth comes from fill + blur, not from
  heavy strokes or drop shadows.
- **Motion:** `150ms ease-out`. Hover raises fill (`--glass` →
  `--glass-raised`), never `translateY` on panels. The only looping
  animation permitted is a ~2s opacity pulse on live/critical indicators.
  Everything respects `prefers-reduced-motion: reduce`.
- **Focus:** every interactive element gets a visible `2px` `--accent`
  focus-visible ring. Non-negotiable.

---

## 5. Layout — reworked from PHASE5_BUILD_PLAN §8

§8 drew feed-left / graph-right over a full-width alert strip. That strip
gives each alert one table row, which is the wrong shape for this product:
the per-alert **"why"** (`StreamingScorer.explain()` — *"bytes 47σ above
baseline"*) is the explainability deliverable, and a table cell cannot
hold it. The blast-radius graph is likewise the differentiator and
deserves the largest region.

```
┌──────────────────────────────────────────────────────────────────────┐
│ AEGIS ●LIVE   events/s 17 │ alerts 3 │ risk 62 │ [speed] [inject]    │  56px
├───────────────┬──────────────────────────────────┬───────────────────┤
│ TELEMETRY     │  CITY INFRASTRUCTURE             │ ACTIVE ALERTS     │
│ (rail, 340px) │  (hero, flex — largest region)   │ (rail, 380px)     │
│               │                                  │                   │
│ mono log,     │  force-directed graph,           │ stacked cards,    │
│ autoscroll,   │  nodes pulse on anomaly,         │ severity rail,    │
│ 200-row cap   │  cascade edges animate on CII    │ asset · blast     │
│               │                                  │ radius · WHY ·    │
│               │                                  │ [ack]             │
└───────────────┴──────────────────────────────────┴───────────────────┘
```

Three changes from §8, each for a reason:

1. **Alerts become a column, not a strip.** Vertical room lets each alert
   card carry its explanation as real prose plus its blast-radius list —
   the explainability requirement, actually visible.
2. **The graph is the hero region**, since blast radius is what this
   product has that a generic IDS does not.
3. **Injection stays a small header control**, per §8's own instruction
   that it is the demo's second act, not the premise.

Below `1280px`, the alerts rail drops beneath the graph; below `900px`,
all three stack. The console targets desktop — mobile is not a goal, but
it must not be broken.

---

## 6. Component patterns

- **Panel** — glass fill, 1px `--glass-border`, `4px` radius, `16px`
  padding, uppercase 11px header label in `--text-dim` with a hairline
  divider beneath.
- **Stat chip** (header) — mono value + uppercase micro-label; value
  colored by its own semantic scale, not by `--accent`.
- **Feed row** — `28px` tall, mono, columns `time · src → dst · glyph`.
  Status glyph is a geometric SVG (● normal, ▲ warning, ■ critical) —
  **no emoji**. Fresh rows at full opacity fading to `--text-mute` with
  age. Anomalous rows get a 2px left border in their severity color.
- **Alert card** — 2px left border in severity color, severity badge,
  asset in mono, one-line "why", collapsible blast-radius list, `ACK`
  button. Acknowledged cards drop to ~55% opacity and lose the border
  accent rather than disappearing.
- **Graph node** — circle for infrastructure, diamond for financial
  (`--financial`), larger outlined ring for gateway/chokepoint nodes.
  Compromised node pulses `--sev-critical` with a soft radial glow;
  cascade edges animate outward along the CII path.
- **Table** — alternating `transparent` / `--glass` rows, 1px dividers,
  uppercase 11px header, `tabular-nums` throughout.
- **Empty and error states are required, not optional**, for every
  data-bound panel — "waiting for stream", "no active alerts",
  "disconnected — retrying". A blank panel during a demo reads as broken.

---

## 7. Accessibility floor

- Body text ≥ 4.5:1 against its actual composited backdrop — check
  against the blurred glass over the ground, not against `--ground`.
- Severity is **never** communicated by color alone: always pair with a
  glyph or text label (colorblind operators, and projector color shift).
- Full keyboard reachability with visible focus for feed, alerts, ack,
  and every header control.
- `prefers-reduced-motion` disables pulses, cascade animation, and
  autoscroll easing.
