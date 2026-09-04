# AEGIS Judge-Room Dossier — Third Edition Update (2026-09-04)

**Live dossier (updated in place, same link as before):**
https://claude.ai/code/artifact/10b258b4-3f33-44b0-8974-f6880cca0038

This file is a companion changelog, not a replacement for the dossier —
read the artifact for the full audit. This is "what changed since the
second edition (2026-09-03), in one page."

## What's new: a full IPS (prevention) layer

Everything up through the second edition covered **detection only** —
five channels scoring traffic, fused into one opinion, that could at
most raise an alert. This edition adds one more stage downstream:

```
Traffic → Hybrid IDS → Detection Fusion → Risk + CII
        → IPS Policy Engine → Prevention Decision
        → Enforcement Adapter → Audit / Persistence / Alert / WS / UI
```

`backend/ips/` — a policy engine that decides **observe / alert /
rate-limit / block / quarantine**, an enforcement adapter behind a
swappable interface, a persisted audit trail, three new REST routes, a
live WebSocket envelope, and a new console panel.

## The one rule that matters most

**A single detector firing, however confident, can never trigger a
block.** Active prevention (rate-limit/block/quarantine) requires either
the honeytoken's unconditional signal, or two or more independently
fired detectors agreeing. This is a detector-*count* floor, not a score
threshold — it cannot be bypassed by relaxing thresholds elsewhere. This
is the concrete, load-bearing answer to "never block every anomaly
automatically."

## Ships safe by default

| Control | Default | What it means |
|---|---|---|
| `ips_enabled` | **False** | The whole layer is off — zero effect on the running system |
| `ips_dry_run` | **True** | Even when enabled, it decides and records but never actually enforces |
| Action TTL | 15m / 30m / 1h | Nothing stays "blocked" forever without review |
| Rollback | `POST /api/ips/actions/{id}/rollback` | A real, tested manual override — 409 if there's nothing active to undo |
| Duplicate protection | built in | Repeated identical decisions are suppressed, not re-logged every batch |
| Enforcement failure | fails open | A bug in the enforcement adapter never crashes ingestion |

## Verified, not just built

Driven directly against the real backend (real ML scorer, real Postgres,
real detectors) over 20,000 real attack-traffic flows with the layer
turned on: 3 decisions made, 11 duplicate decisions correctly suppressed,
0 failures. Then verified end-to-end through the actual browser console —
a decision appeared in the new **IPS Prevention** panel, and clicking
**Roll back** correctly updated it live.

That live verification also caught a real bug: a rollback method was
reading a database field *after* its connection had already closed —
invisible to the fast unit tests and invisible in production (which
happens to be configured in a way that papered over it), only surfaced
once a test used a strict, realistic database session. Found and fixed
in this pass — see the dossier's Part 7 for the full story.

## What's still open (stated honestly, not hidden)

- The layer's decisions have **not** been precision/recall-tested with
  real enforcement turned on — that's a different, separate measurement
  from "does it run stably," which this pass did complete.
- The beaconing detector's reliability weight is still an admitted,
  unmeasured placeholder (unchanged from the second edition).

## Test suite

**720 passed / 15 skipped** (default, no database) · **735 passed / 0
skipped** (with a live Postgres instance) — up from 680/665 before this
pass, entirely new IPS regression coverage, zero regressions anywhere
else.
