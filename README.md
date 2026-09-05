# AEGIS: Anomalous Event Graph Intelligence System

AEGIS is a cyber-physical risk analytics platform for a smart city —
municipal infrastructure plus municipal financial systems. It answers two
questions:

1. **Detection** — which telemetry events are anomalous?
2. **Blast radius** — if this asset is compromised, what else falls over?

It runs on **real captured network traffic** (CIC-IDS2017, 3.2 GB across
eight capture days), replayed in true chronological order. Nothing in the
live path is synthetic.

---

## Two consoles

**Operations Console** (`backend/` + `frontend/`) — the live view. Real
CIC-IDS2017 traffic replayed in timestamp order, scored by a pre-fitted
model, persisted to PostgreSQL, streamed over a WebSocket, and rendered in
a Next.js console.

The city graph shows **11 sector nodes by default** — clustered around a
central operations hub and sized by how many assets each holds — and
expands to all **50 individual assets** on demand, because 50 labelled
nodes are not legible in a side panel but are perfectly legible full
screen. Alongside it: a live telemetry feed, an alerts panel with
per-alert explanations and an acknowledge flow, a per-sector health strip,
and live replay progress against the capture day.

**Research Console** (`src/aegis_demo.py`, Streamlit) — the analytics
workbench: dataset selection, detector benchmarking, and the evaluation
harness that produces the honest precision/recall/F1/AUC numbers quoted
below.

Both run on the same engine. The Research Console is where you check the
work; the Operations Console is where you watch it happen.

---

## What it does

- **Six detection channels, correlated into one fused decision.** The
  original three, all scored live every batch and persisted as their own
  `event_scores` row per event: an unsupervised Isolation Forest
  (novel-threat channel), a supervised RandomForest (known-threat
  channel, fit once at build time on real labelled attack traffic —
  `backend/warmup_supervised.py`), and a honeytoken tripwire (deception
  channel). Their real, measured strengths and limits are in §*Honest
  limitations* — in particular, watch the volumetric and known-threat
  channels disagree live: inject a real Bot scenario (`POST /api/inject`)
  and the Isolation Forest says "normal" while the RandomForest flags it
  at 0.96+ confidence, the exact, concrete shape of the paradigm gap this
  section describes. Three more feed the Hybrid IDS fusion layer
  (`docs/FEATURES.md` §3): a signature engine (declarative rules over
  flow metadata), a beaconing detector (inter-arrival timing regularity),
  and **T-GNN** (topological channel) — lightweight structural-embedding
  anomaly detection over a sliding-window traffic graph (NetworkX +
  IsolationForest, honestly not a full GNN) that catches an attacker who
  is volumetrically quiet and rule-compliant but topologically unusual.
- **Mandatory gateway topology** — every path to a high-criticality asset
  is structurally rewritten to route through a Purdue-zone gateway node
  (`src/graph_manager.py`). It is a chokepoint in the graph itself, not a
  passive monitoring tap an attacker could route around.
- **Honeytoken tripwire** — a credential seeded in each gateway zone with
  zero legitimate use anywhere in the system. Any use is unambiguous
  compromise by construction: no false positives are possible, and it
  needs no training data. Its lead time over volumetric detection measures
  **58.4 s mean** — see the qualification in §*Honest limitations*, because
  that number is **not** measured on real capture traffic.
- **Cascading Impact Index (CII)** — Monte Carlo simulation over a curated
  50-node dependency graph, reporting a **distribution** (median, p5, p95),
  not a point estimate. Scores are a *fraction of the city's criticality
  mass*, so 0.22 reads as "about a fifth of the city falls over".
- **Operator what-if injection** — `POST /api/inject` replays **real
  labelled attack flows** from the capture (1,966 Bot, 128k DDoS, 159k
  PortScan) re-targeted onto a chosen asset. Injected events are tagged
  `batch_origin=injected` end to end and badged in the UI, so an operator
  hypothesis can never be mistaken for observed telemetry.
- **A risk index that is defined, not asserted.** The header's `RISK`
  figure is `Σ(severity_weight × asset_criticality)` over **unacknowledged**
  alerts, normalised against a documented presentation scale — the formula
  is in the UI tooltip. Acknowledging an alert visibly lowers it, which is
  what makes it an operator tool rather than a decoration. It is
  deliberately **not** built on CII (see *Honest limitations*).
- **The alert policy is visible, not hidden.** The console reports how
  many anomalies it detected and deliberately did *not* page you for —
  typically a few hundred against one real alert. Suppression is a stated
  policy with a measured justification, not silent filtering.
- **Honest evaluation** (`src/evaluation/`) — segment-wise recall with
  row-wise precision for ICS time series (a deliberate rejection of the
  "point-adjust" metric, which can make random noise look
  state-of-the-art), and a guard that raises rather than silently
  reporting zeros on a degenerate split.

---

## Honest limitations

These are measured, reproducible, and deliberately published. A benchmark
showing a detector's real limits is more credible than one showing only
its wins.

| Channel | Known threat | Novel threat | Training data |
|---|---|---|---|
| Unsupervised (Isolation Forest) | weak (**P ≈ 0.02**) | weak | benign baseline |
| Supervised (RandomForest) | **strong (P ≈ 0.996)** | **zero** | labelled attacks |
| Honeytoken tripwire | perfect | **perfect** | **none** |

- **The unsupervised detector is weak on real traffic.** On real replayed
  friday-morning data it produced **5 true positives against 811 false
  positives**. Bot C2 beaconing is *smaller* than benign traffic (median 6
  bytes vs 70), so an outlier detector over volume looks in the wrong
  direction. Feature engineering made it worse, not better (AUC 0.67 →
  0.21). Full write-up: `docs/DETECTION_STUDY.md`.
- **Because of that, volumetric anomalies do not page an operator by
  default.** They are still scored, persisted, and shown in the live feed —
  the console reports how many it suppressed and why. A typical run shows
  **265 suppressed against 1 alert raised**. The alert policy is visible,
  not hidden.
- **The supervised detector is blind to what it has not seen.** Honest
  temporal-split evaluation: AUC 0.847, precision 0.996, recall 0.595. On a
  novel attack family its precision is **0.000**. It is reported with those
  numbers, never with same-distribution self-test figures.
- **Real capture traffic does not intersect the modelled city.** Measured:
  **0 of 20,000** real source IPs resolve to a dependency-graph asset —
  CIC-IDS2017 hosts are `192.168.10.x`, the curated assets are `10.0.1.x`.
  The graph therefore draws two honestly *disconnected* layers and says so
  on screen, rather than inventing edges to make a cascade look connected.
- **A CII median of 0.0 is common and truthful.** It means more than half
  the Monte Carlo iterations propagated nothing — the right answer for a
  weakly-coupled leaf. Read the p5–p95 interval, not just the median: an
  asset can honestly report median 0.0 with p95 0.185.
- **The 58.4 s tripwire lead time is measured on scripted attack
  timelines, not on real capture traffic** (`src/evaluation/lead_time.py`).
  That is a real constraint, not an evasion: a honeytoken is AEGIS's own
  planted credential, so `is_honeytoken_use` is false on every row of a
  2017 public capture by definition, and running the tripwire through the
  ordinary precision/recall harness would trivially predict "normal" for
  the entire dataset — a meaningless result rather than a poor one. Lead
  time is the tripwire's own metric on its own two-stage recon-then-exfil
  timeline. Read it as evidence about *when in an attack the deception
  layer fires*, not as a detection rate on captured traffic.

---

## SDG alignment

**SDG 9 — Industry, Innovation and Infrastructure.** Municipal
infrastructure is increasingly cyber-physical, and its failures cascade:
compromising a payment gateway can reach a bank interface and then a
welfare disbursement system. AEGIS makes that dependency structure
explicit and quantifies it, so resilience can be reasoned about before an
incident rather than reconstructed after one. The Purdue-zone gateway
model reflects how operational-technology networks are actually segmented.

**SDG 11 — Sustainable Cities and Communities.** The assets modelled are
the ones cities actually run: water treatment, power substations, traffic
control, emergency dispatch, hospital networks, and the financial systems
that pay for them. The blast-radius view is designed to answer a question
a city operator genuinely has — *which of my services stop working, and in
what order* — including the socially critical ones, which is why welfare
disbursement sits in the dependency graph alongside the power grid.

The honesty posture above is part of this claim, not separate from it.
Infrastructure decisions made on an overstated detector are worse than
decisions made with none.

---

## Quick start

**Prerequisites:** Python 3.11–3.13, Node 20+, PostgreSQL 16.

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-backend.txt
```

Place the CIC-IDS2017 CSVs under `datasets/` (see `docs/DATASETS.md` — they
are gitignored and must be obtained separately).

### Operations Console

```bash
cp .env.example .env
python -m backend.init_db
python -m backend.warmup
```

`warmup` fits the streaming model **once**, at build time, and saves the
artifact. The console deliberately refuses to fit a model on live stream
data — that would let the baseline drift toward the attack.

```bash
PYTHONPATH=src uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

```bash
npm --prefix frontend install && npm --prefix frontend run dev
```

Open <http://localhost:3000>, then start a replay:

```bash
curl -X POST http://127.0.0.1:8000/api/replay/start -H 'Content-Type: application/json' -d '{"dataset":"friday-morning","speed":20.0}'
```

### Research Console

```bash
streamlit run src/aegis_demo.py
```

---

## Architecture

```
CIC-IDS2017 CSVs
      │  ReplayFlowReader — real IPs, corrected timestamps, chronological
      ▼
ReplayEngine ──► IngestPipeline ──► PostgreSQL
  paced,           score · resolve      events · scores
  batched          fuse · CII           alerts · cii_snapshots
                        │
                        └──► WS /ws/stream ──► Next.js console
                                                feed │ graph │ alerts
```

The detection and risk math are unchanged from the research phase — the
operations layer packages the engine, it does not replace it.

### API

Twelve REST routes plus the stream. Interactive docs at
<http://127.0.0.1:8000/docs> once the backend is running.

| | |
|---|---|
| `GET /api/health` | liveness, database, whether the model artifact loaded |
| `GET /api/topology` | the 50-node graph — nodes, edges, sectors |
| `GET /api/events` | recent events, cursor-paged |
| `GET /api/alerts` · `POST /api/alerts/{alert_id}/ack` | alert list and acknowledge |
| `GET /api/cii/{asset}` | on-demand blast radius for any asset |
| `GET /api/stats` | ingest counters, replay status, alert counts, risk index |
| `POST /api/replay/start` · `stop` · `speed` | replay control |
| `GET /api/inject/scenarios` · `POST /api/inject` | what-if scenarios |
| `WS /ws/stream` | live `event` / `alert` / `cii` envelopes |

`GET /api/events` takes `since` as an **event id, not a timestamp** —
hundreds of events share one timestamp on a minute-bucketed capture day,
so a time cursor silently loses rows. That mistake cost two separate
rounds of debugging here; the constraint is documented on the route.

---

## Tests

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

**538 passed, 13 skipped.** Skips are real-dataset tests gated on
`datasets/` being present, plus live-database tests gated on
`AEGIS_TEST_LIVE_DB=1`.

---

## Documentation

| Doc | What it covers |
|---|---|
| `PLAN_MASTER.md` | authoritative plan, architecture decisions, phase history |
| `docs/PHASE5_STATE.md` | ticket-by-ticket build record and known issues |
| `docs/DETECTION_STUDY.md` | the detector measurements behind §*Honest limitations* |
| `docs/SETUP.md` | installation, including PostgreSQL |
| `docs/EVALUATION.md` | evaluation protocol and how to reproduce the metrics |
| `docs/DATA_SCHEMA.md` | canonical event schema and the PostgreSQL schema |
| `docs/DATASETS.md` | dataset sources and licences |
| `docs/DESIGN_CONSOLE.md` | Operations Console design tokens |
| `docs/DESIGN.md` | Research Console design tokens |

Treat the code as authoritative where a doc disagrees with it. This
project has been through several phase migrations, and stale docs
describing superseded designs are a known, actively-tracked failure mode
here. If you find one, fix it.

---

## Contributors

- **Ishan Petkar** ([@Ishan-Petkar](https://github.com/Ishan-Petkar)) — Lead Developer & Architect

