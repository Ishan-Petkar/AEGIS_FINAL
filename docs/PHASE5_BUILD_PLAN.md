# Phase 5 Build Plan — Live Operations Console

**Goal:** turn AEGIS from a batch research demo into a system that *looks and behaves like production*: real telemetry streaming continuously, persisted to Postgres, served through an API, rendered live in a Next.js operations console.

**Timeline:** 8 days · **Team:** 3 · **Workflow:** GitHub issues + PRs

**Non-goal:** changing the detection or risk math. Phases 1–3 (229 passing tests, real-dataset evaluation, honest metrics) are the asset we are *packaging*, not replacing. `src/` stays intact.

---

## 1. Why this, and why now

The hackathon problem statement names four things explicitly. Here is where we stand:

| Problem statement requirement | Status before Phase 5 | After Phase 5 |
|---|---|---|
| Ingest structured/semi-structured data | ✅ Canonical schema + 4 adapters | ✅ unchanged, now continuous |
| AI/ML anomaly detection | ✅ Strong — real ground truth, honest metrics | ✅ unchanged |
| **Actionable insights: risk scores, alerts, dashboards** | ⚠️ Scores + dashboard, but **no alert mechanism** | ✅ `alerts` table + ack workflow |
| **Scalability for real-world environments** | ❌ Batch, static, 35 synthetic nodes | ✅ Streaming, persisted, real traffic |
| **Explainability** | ⚠️ Graph-level yes, model-level no | ✅ per-alert feature attribution |
| SDG 9 / SDG 11 alignment | ❌ Not mentioned anywhere | ✅ README + UI |

Two of those gaps (**scalability**, **alerts**) are named directly in the brief. That is what makes this phase worth 8 days rather than polish work.

---

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  REPLAY ENGINE  (backend/replay.py)                              │
│  Reads real CIC-IDS2017 rows in timestamp order, emits them at   │
│  a compressed pace (configurable speed multiplier, default 20×)  │
└──────────────────────────┬─────────────────────────────────────┘
                            │ one event at a time
                            ▼
┌────────────────────────────────────────────────────────────────┐
│  FASTAPI BACKEND  (backend/)                                     │
│   • scores each event via src/core/streaming.py (pre-fitted      │
│     model — never refits mid-stream)                             │
│   • resolves identity via datasets.asset_registry                │
│   • on anomaly → CII via cii_calculator (cached/debounced)       │
│   • writes to Postgres · pushes over WebSocket                   │
└──────────┬──────────────────────────────────┬──────────────────┘
           │                                  │
           ▼                                  ▼
   ┌──────────────┐                 ┌────────────────────┐
   │  POSTGRESQL   │                 │  WEBSOCKET /ws/stream│
   │ events·scores │                 └─────────┬──────────┘
   │ alerts·cii    │                           │
   └──────────────┘                           ▼
                                    ┌────────────────────────┐
                                    │  NEXT.JS CONSOLE         │
                                    │  live feed │ city graph  │
                                    │  alerts · blast radius   │
                                    └────────────────────────┘
```

### Repo layout

```
aegis-project/
├── src/                    ← UNCHANGED except one new file
│   ├── core/streaming.py   ← NEW: fit-once / score-per-event
│   └── ...                    (pipeline, detectors, deception,
│                               evaluation, datasets, cii_calculator)
├── backend/                ← NEW: FastAPI service
│   ├── main.py                app + routes + websocket
│   ├── db.py                  SQLAlchemy models + session
│   ├── replay.py              dataset replay engine
│   ├── ingest.py              score → persist → broadcast
│   └── schemas.py             pydantic request/response models
├── frontend/               ← NEW: Next.js console
│   ├── app/
│   ├── components/
│   └── lib/
├── src/aegis_demo.py       ← KEPT (see §3)
└── docs/PHASE5_BUILD_PLAN.md
```

---

## 3. Keep the Streamlit app — it becomes the second surface

Do **not** delete `aegis_demo.py`. It holds the Phase 3 evaluation panel: real-ground-truth benchmarks, segment-wise ICS metrics, tripwire lead-time. That is our credibility evidence and no other team will have it.

Reframe as two surfaces on one engine:

- **Operations Console** (Next.js) — what a city SOC operator uses. Live, real, no toy controls.
- **Research Console** (Streamlit) — how we prove the detection is honest. Benchmarks, methodology, evaluation.

That pairing is a *stronger* pitch than one app, and it costs zero extra engineering.

---

## 4. The one real engine change: fit-once, score-per-event

`ml_engine.preprocess_features()` calls `scaler.fit_transform()` — it fits a new scaler every call. Correct for batch, **wrong for streaming** (refitting per event means the baseline drifts to match the attack, and scores become meaningless).

New module `src/core/streaming.py`:

```python
class StreamingScorer:
    """Fit once on a historical warmup window, then score events as they arrive."""

    @classmethod
    def fit_from_warmup(cls, df, features=None, n_estimators=None,
                        contamination=None) -> "StreamingScorer"

    def save(self, path) -> None          # joblib — warmup happens at build time, not demo time
    @classmethod
    def load(cls, path) -> "StreamingScorer"

    def score_batch(self, df) -> pd.DataFrame   # transform-only, never fit
    def score_event(self, event: dict) -> dict  # single event convenience wrapper

    def explain(self, event: dict) -> list[dict]
        # per-feature deviation vs the warmup baseline, e.g.
        # [{"feature": "bytes", "value": 5.0e8, "baseline_mean": 12_400,
        #   "sigma": 47.2}, ...]  → this is our model-level explainability
```

Rules:
- Warmup is fit on **benign-only** rows from a historical slice, same discipline as `evaluation/`.
- The scaler is fit exactly once and reused via `transform()`. Never `fit_transform()` in the stream path.
- Persist to disk at build time so the demo machine never trains live.
- `explain()` is cheap (z-score vs warmup baseline) and closes the model-explainability gap without SHAP.

**Tripwire fusion is unchanged** — reuse the OR + confidence-escalation logic already in `core/pipeline.py`.

---

## 5. Node-count problem (why the graph won't explode)

Real CIC-IDS2017 has thousands of distinct IPs. Rendering thousands of nodes is unreadable and slow. The graph shows **assets, not packets**:

1. Curated smart-city assets (11) + Purdue gateways (6) — from `config.py` / `graph_manager.py`
2. Real IPs map onto those via `AssetRegistry.resolve()` (already built, already tested)
3. Anything unresolved is **aggregated into `/24` subnet cluster nodes** with a count badge — `External_45.227.254.0/24 ×1,284` — instead of one node per IP

Result: a readable ~30–60 node graph that is nonetheless driven entirely by real traffic. The live feed panel still shows raw per-event detail, so nothing is hidden.

This is also the honest answer to "why so few nodes" — the topology is the *city asset model*, and the volume lives in the feed and the counters.

---

## 6. Database schema (Postgres)

```sql
assets(id, name UNIQUE, ip, type, criticality, purdue_level, is_gateway)

events(id, ts, observed_at, source_id, destination_id,
       source_asset, destination_asset, protocol,
       bytes, packets, duration_sec, signal_type,
       source_dataset, raw JSONB)

event_scores(id, event_id FK, detector, raw_score, calibrated_score,
             is_anomaly, confidence)

cii_snapshots(id, ts, origin_asset, cii_median, cii_p5, cii_p95,
              impacted JSONB, hop_details JSONB, trigger_event_id FK)

alerts(id, ts, severity, asset, title, detail,
       explanation JSONB,          -- from StreamingScorer.explain()
       cii_snapshot_id FK, acknowledged BOOL, acknowledged_at)
```

Indexes: `events(ts DESC)`, `events(source_asset)`, `alerts(acknowledged, ts DESC)`, `event_scores(event_id)`.

**Retention:** cap `events` at the most recent N (e.g. 500k) via a periodic delete — a demo shouldn't accumulate unbounded rows.

---

## 7. API contract

```
GET  /api/health
GET  /api/topology                 → nodes + dependency edges (graph_manager.build_graph)
GET  /api/events?limit=&since=     → recent events, paged
GET  /api/alerts?acknowledged=     → alert list
POST /api/alerts/{id}/ack          → acknowledge
GET  /api/cii/{asset}              → on-demand blast radius (compute_cascading_impact_full)
GET  /api/stats                    → header counters (events/s, alerts, risk index)
POST /api/replay/start             → {dataset, speed}
POST /api/replay/stop
POST /api/replay/speed             → {multiplier}
POST /api/inject                   → {scenario} — scripted attack (generate_scripted_attack)
WS   /ws/stream                    → live push
```

WebSocket envelope — one shape, typed:

```json
{ "type": "event" | "alert" | "cii" | "stats", "data": { ... } }
```

---

## 8. Frontend layout

```
┌───────────────────────────────────────────────────────────────┐
│ AEGIS · LIVE ●   events/s 17 · alerts 3 · risk 62/100 · [speed]│
├──────────────────────────────┬────────────────────────────────┤
│  LIVE TELEMETRY FEED          │  CITY INFRASTRUCTURE GRAPH      │
│  autoscroll, buffer-capped    │  nodes pulse on anomaly,        │
│  14:23:01 10.0.1.20→198… PASS │  cascade edges animate on CII   │
│  14:23:02 10.0.1.16→45.2… ⚠   │                                 │
├──────────────────────────────┴────────────────────────────────┤
│  ACTIVE ALERTS — severity · asset · blast radius · why · [ack] │
└───────────────────────────────────────────────────────────────┘
```

- **Graph:** `react-force-graph-2d` (canvas, handles this scale comfortably)
- **Feed:** cap the client buffer at ~200 rows — no virtualization dependency needed
- **"Why" on each alert:** renders `StreamingScorer.explain()` — *"bytes 47σ above baseline"* — which is the explainability requirement, visible
- Attack injection lives in a **small tucked-away control**, not center stage. It is the demo's second act, not the premise.

---

## 9. Ticket breakdown

Each is one PR. `[A]` backend/DB · `[B]` frontend · `[C]` infra/integration.

**Day 0–1 — foundations (nobody blocked)**
- `#1 [C]` Postgres via Homebrew + `.env.example` + connection docs; optional `docker-compose.yml` for parity
- `#2 [A]` SQLAlchemy models + migrations for §6 schema; seed `assets` from `config.SMART_CITY_ASSETS`
- `#3 [B]` Next.js scaffold, dark theme tokens ported from `docs/DESIGN.md`, app shell
- `#4 [B]` **Mock WebSocket server** emitting fake events at a set rate — unblocks all frontend work immediately
- `#5 [A]` `src/core/streaming.py` — `StreamingScorer` with fit/save/load/score/explain + unit tests

**Day 2–3 — the pipes**
- `#6 [C]` Replay engine: real CIC-IDS2017 in timestamp order, speed multiplier, start/stop
- `#7 [A]` `backend/ingest.py` — score → persist → broadcast; CII debounce/cache
- `#8 [A]` FastAPI routes from §7 (except inject)
- `#9 [A]` WebSocket endpoint + envelope
- `#10 [B]` Live feed component (against mock)
- `#11 [B]` Graph component + `/24` cluster aggregation (against mock)

**Day 4 — integration day (all three)**
- `#12` Frontend swaps mock → real WS; end-to-end live from replay to browser
- `#13 [C]` `POST /api/inject` wired to `generate_scripted_attack`

**Day 5 — the payoff**
- `#14 [B]` CII cascade animation on the graph
- `#15 [B]` Alerts panel + ack flow + per-alert "why" (explainability)
- `#16 [A]` `/api/stats` counters feeding the header

**Day 6 — credibility + story**
- `#17` SDG 9 / SDG 11 section in README + UI footer card
- `#18` README rewrite: architecture diagram, both consoles, how to run
- `#19` Styling pass, empty/error states, reconnect handling

**Day 7–8 — prove it works**
- `#20` Full dry-run demo, fix what breaks
- `#21` Pitch deck + rehearsal · buffer

---

## 10. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Frontend blocked waiting on backend | `#4` mock WS server on Day 1 — B never waits |
| R2 | Docker not installed; setup eats a day | Homebrew Postgres is the primary path; Compose is optional parity |
| R3 | Integration slips past Day 4 | Contract (§6/§7/§8) is decided *now*, in writing — both sides build to it |
| R4 | Demo machine trains model live and stalls | Warmup fitted at build time, `joblib`-persisted, loaded at boot |
| R5 | Graph unreadable at real data volume | `/24` cluster aggregation (§5), decided up front |
| R6 | WS + Next.js SSR friction | Client component + `useEffect`; SSE is the fallback if WS fights us |
| R7 | Scope creep kills Day 7 buffer | Days 7–8 are buffer *by design* — protect them |

---

## 11. Demo script (what wins)

1. Open on the **live console**. Real traffic streaming, graph calm, counters ticking.
   *"This is real CIC-IDS2017 network telemetry replayed at 20×. Not a simulation — recorded traffic from real infrastructure."*
2. Anomalies surface naturally from the real data. Nodes flicker amber.
3. **Inject Payment Gateway Breach.** The honeytoken tripwire fires on the *recon* stage — an alert with a full blast radius appears **before any exfiltration happens**.
4. CII cascade animates across the city graph: payment gateway → bank API → welfare system.
5. Open an alert → *"bytes 47σ above baseline"* → explainability, not a black box.
6. Switch to the **research console**: *"and here's why you should believe the detection"* — real ground truth, segment-wise ICS metrics, 58.4s mean tripwire lead time, degenerate-split guard.
7. Close on SDG 9 / SDG 11: resilient infrastructure, safer cities.

The arc is: **real → live → predictive → explainable → proven.**

---

## 12. Definition of done

- [ ] Real dataset streams continuously into Postgres; events survive a restart
- [ ] Next.js console shows live feed + live graph with zero page refresh
- [ ] An anomaly produces a persisted **alert** with blast radius and a human-readable "why"
- [ ] Attack injection works on demand and the tripwire fires before exfil
- [ ] `src/` engine tests still pass (229/229) — the engine was packaged, not modified
- [ ] README covers architecture, both consoles, setup, and SDG alignment
- [ ] Full demo runs start-to-finish on the demo machine with no internet dependency
