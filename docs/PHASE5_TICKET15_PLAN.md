# Ticket #15 Plan — alerts panel, ack flow, per-alert "why"

Planning authority: this document. `docs/DESIGN_CONSOLE.md` governs visuals.

---

## 1. Scope

Replace `AlertsRail`'s static placeholder cards with the real alert
stream, an acknowledge flow, and the per-alert explanation.

`docs/PHASE5_BUILD_PLAN.md` §8 calls the per-alert "why" **the
explainability requirement, visible** — this ticket is where that claim
is either honoured or quietly dropped.

**OUT of scope:** `/api/stats` (#16), README/SDG (#17/#18), the graph
(#14, done), the camera-framing item deferred to #19.

---

## 2. Data sources — merge REST history with the live stream (D15-1)

- On mount, load existing alerts via `GET /api/alerts` (bounded `limit`).
  Without this, a freshly-loaded console shows an empty alerts panel even
  though alerts exist in Postgres — the operator's durable record.
- Merge live `alert` envelopes from the shared `useStream()` context.
- **Dedupe by `id`.** An alert can legitimately arrive from both sources
  (loaded via REST, then re-broadcast). Never render it twice.
- Order newest first, matching the API's `ts DESC, id DESC`.
- Never call `useEventStream()` directly — use the shared context (the
  duplicate-socket defect fixed in #10).

---

## 3. The "why" must not fabricate a statistic (D15-2)

`StreamingScorer.explain()` (Ticket #5) returns:
```
{ "top_feature": "<name>",
  "features": [ {"name","z","degenerate_baseline", ...}, ... ] }
```
sorted by `|z|` descending, **always including every feature**.

Critically: a feature whose warmup variance was zero reports
`z: null` and `degenerate_baseline: true`, and is described in raw
units. Ticket #5 did this deliberately — sklearn substitutes
`scale_=1.0` for a zero scale, so a naive z there "would be a raw-unit
number wearing a sigma label — a fabricated statistic."

**The UI must honour that:**
- Render `z` as `Nσ` **only** when `z` is a real number.
- For `degenerate_baseline` features, never print a sigma. Show the raw
  value and mark the baseline as degenerate.
- Do not coerce `null` to `0`. A missing z is not a zero deviation.
- Lead with `top_feature` (e.g. *"bytes 47σ above baseline"*) — that one
  line is the explainability payoff.
- Show the leading few features by default with the rest expandable;
  never silently truncate to the point of hiding a degenerate one.

---

## 4. Ack flow (D15-3)

- `POST /api/alerts/{id}/ack`. The route is already idempotent and
  preserves the **first** `acknowledged_at` (Ticket #8).
- Optimistic update is fine, but **must roll back on failure** and
  surface the error. An alert that shows as acknowledged when the write
  failed is a false operator record — worse than a slow UI.
- Acknowledged cards drop to reduced opacity and lose the severity accent
  rather than disappearing (`DESIGN_CONSOLE.md` §6) — the record persists.
- The ACK control must be keyboard reachable with a visible focus ring.
- Disable/spinner the control while in flight so a double-click cannot
  fire two writes.

---

## 5. Blast radius on the card

Each alert carries `cii_snapshot_id`. Where present, the card's blast
radius section should show the **real** impacted assets. Fetch on expand
(`GET /api/cii/{asset}` or the snapshot) rather than eagerly for every
card — at demo speeds there can be many alerts.

If `cii_snapshot_id` is null (e.g. the asset was not in the dependency
graph — K8), say so plainly rather than showing an empty list that reads
as "no impact". "No blast radius computed — asset not in the dependency
graph" is true; a blank list is misleading.

---

## 6. States

Loading, empty ("no active alerts" — a real and *good* state), and error
are all required. Copy must be literally true — a false status string has
been a defect twice in this project.

---

## 7. Verification

```bash
cd frontend && npx tsc --noEmit && npm run lint && npm run build
git status --short src/ backend/     # empty
```

Against the real stream:
1. Load with existing alerts in Postgres → they appear (REST history).
2. `POST /api/inject {"scenario":"honeytoken","target_asset":"City_Payment_Gateway"}`
   → a new critical alert appears live, **not duplicated** with the REST
   copy.
3. The card shows a real "why" line from `explanation.top_feature` — cite
   the actual text rendered.
4. Click ACK → card visually acknowledges; confirm in Postgres that
   `acknowledged = true` and `acknowledged_at` is set. Re-ack → the
   original `acknowledged_at` is unchanged (idempotency, verified in DB).
5. Expand blast radius → impacted assets match the CII snapshot.
6. Force an ack failure (stop the backend) → the optimistic update rolls
   back and an error is surfaced; the card does **not** stay falsely acked.
7. Zero console errors in a fresh tab. Screenshot.

---

## 8. Constraints

- Do not modify `src/` or `backend/`.
- No raw hex/`rgba()` — tokens only.
- Real data only; mock stays down.
- Commit nothing.
