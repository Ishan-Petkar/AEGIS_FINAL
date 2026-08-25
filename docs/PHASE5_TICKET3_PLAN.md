# Ticket #3 Plan — Next.js scaffold + theme tokens + app shell

Planning authority: this document, plus `docs/DESIGN_CONSOLE.md` for all
visual decisions. Follow both; raise disagreements rather than diverging
silently.

---

## 1. Scope

**In scope:**
1. `frontend/` — Next.js (App Router) + TypeScript + Tailwind, created
   under the repo root as a sibling of `backend/` and `src/`.
2. `docs/DESIGN_CONSOLE.md`'s tokens ported into the Tailwind theme as
   **named tokens**, driven by CSS custom properties.
3. The static app shell: header bar + three-region body + panel primitives,
   laid out per `DESIGN_CONSOLE.md` §5, with realistic placeholder content.
4. A typed API client and shared TypeScript types mirroring the Ticket #8
   response schemas.
5. `GET /api/health` + `GET /api/topology` wired for real, to prove the
   frontend↔backend connection end to end.

**OUT of scope — do not build:**
- WebSocket client or live streaming — Ticket #4 (mock server) and #12.
- The force-directed graph itself — Ticket #11. Render a placeholder
  panel with a correct empty state.
- Live feed behaviour (autoscroll, buffering) — Ticket #10. Static
  placeholder rows only.
- Alert ack wiring — Ticket #15. Render the card and a non-functional
  (but focusable, correctly styled) ACK button.
- `POST /api/inject` — Ticket #13. Header button renders disabled.

The deliverable is a **shell that looks right and connects**, not a
working console.

---

## 2. Scaffold decisions

- `npx create-next-app@latest frontend --typescript --tailwind --eslint
  --app --src-dir --import-alias "@/*"` — non-interactive.
- **Do not** add `react-force-graph-2d` yet; it is Ticket #11's
  dependency and pulls a large tree. Keep this scaffold's `node_modules`
  as small as it can be (risk T11: disk).
- Add `frontend/node_modules/`, `frontend/.next/`, `frontend/out/` to the
  repo `.gitignore`. Verify `git status` is clean of build output before
  finishing — a committed `.next/` or `node_modules/` would be a serious
  repo regression.
- Dev server on **port 3000** (matches `BACKEND_SETTINGS.api_cors_origins`,
  which already allows `http://localhost:3000` and `http://127.0.0.1:3000`
  — do not change the backend to suit the frontend).

---

## 3. Token wiring (the part most likely to be done sloppily)

Two layers, in this order:

1. `src/app/globals.css` defines **every** token from
   `DESIGN_CONSOLE.md` §2 as a CSS custom property on `:root`.
2. `tailwind.config.ts` maps those properties to **semantic Tailwind
   names** — e.g. `colors.ground`, `colors.glass.DEFAULT`,
   `colors.text.dim`, `colors.sev.critical`, `colors.accent.DEFAULT`.

Components then write `bg-glass border-glass-border text-text-dim`. A raw
hex or an `rgba()` literal in a component file is a defect — grep for
`#[0-9a-fA-F]{6}` under `src/` before you finish and report the result.

Also required:
- `backdrop-filter` panels must carry an opaque fallback via
  `@supports not (backdrop-filter: blur(1px))`.
- Fonts: Inter + JetBrains Mono via `next/font/google`, exposed as CSS
  variables, with the fallback stacks named in `DESIGN_CONSOLE.md` §3.
- Global `prefers-reduced-motion: reduce` block disabling pulses and
  transitions.
- The console is dark-only by deliberate choice (it is a committed single
  look, per `DESIGN_CONSOLE.md` §1) — so paint `background` and `color`
  explicitly on `body`; do not rely on a `prefers-color-scheme` default.

---

## 4. API client

`src/lib/api.ts` — a thin typed fetch wrapper:
- Base URL from `process.env.NEXT_PUBLIC_API_BASE_URL`, defaulting to
  `http://127.0.0.1:8000`. Add `frontend/.env.local.example` documenting it.
- One exported function per endpoint used in this ticket
  (`getHealth`, `getTopology`), each returning a typed result.
- Non-2xx must **throw a typed error carrying the status**, so panels can
  distinguish "backend down" from "404 unknown asset" — Ticket #8
  deliberately made those different codes and the UI must not flatten them.

`src/lib/types.ts` — TypeScript interfaces mirroring the Ticket #8
Pydantic response models. Derive them from `backend/schemas.py`; do not
invent field names. In particular `EventsResponse` carries
`{ events, has_more }`, and the `since` query parameter is an **event
id**, not a timestamp — Ticket #10 will depend on this being right, and
getting it wrong caused two separate HIGH-severity bugs in Ticket #8.

---

## 5. Components to build

Under `src/components/`, each a server component unless it needs state:

| Component | Notes |
|---|---|
| `AppHeader` | brand, live pulse dot, stat chips, speed control (static), disabled inject button |
| `Panel` | the shared glass primitive: label, hairline divider, children, optional action slot |
| `StatChip` | mono value + micro label, semantic color |
| `TelemetryRail` | panel + ~12 placeholder mono rows, correct glyphs and severity borders |
| `GraphPanel` | panel + centered empty state ("topology loaded — live graph arrives in Ticket #11"), rendering the **real** node/edge counts from `/api/topology` |
| `AlertsRail` | panel + 3 placeholder alert cards, one critical / one warning / one acknowledged, each with a realistic "why" line |
| `ConnectionState` | small header indicator driven by `/api/health`: connected / degraded / unreachable |

Every data-bound panel needs **loading, empty, and error** states
(`DESIGN_CONSOLE.md` §6). The error state must be reachable — verify by
stopping the backend, not by reasoning about it.

---

## 6. Verification (do all of it; report real output)

```bash
cd frontend && npm run build          # must succeed, zero type errors
cd frontend && npx tsc --noEmit       # explicit type gate
cd frontend && npm run lint
```

Then **prove the connection**, do not assert it:

1. Start the backend: `PYTHONPATH=src ../venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000`
2. Start the frontend dev server on 3000.
3. Load the page and confirm `/api/topology` returns **16 nodes / 20
   edges** into the graph panel, and the health indicator reads connected.
4. Confirm no console errors and no CORS errors in the browser.
5. Stop the backend and reload — confirm the error states render and the
   page does not crash or hang.

Report the real numbers seen in the browser. A screenshot of the rendered
shell is the acceptance artifact.

Backend gates must still pass untouched:
```bash
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q   # 483 passed, 13 skipped
git status --short src/                                 # empty (Invariant A)
```

---

## 7. Constraints

- **Invariant A holds for the frontend too:** do not modify `src/` or
  `backend/`. If the frontend needs a backend change, stop and report it
  rather than editing across the boundary.
- Do not commit `node_modules/`, `.next/`, or lockfile churn from a
  different package manager. `package-lock.json` **is** committed.
- No raw hex in components (§3).
- No new backend settings, routes, or CORS origins.
