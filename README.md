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
  channel). Their real, measured strengths and complementary roles are
  detailed in §*Multi-Channel Detection Architecture & Evaluation* —
  in particular, watch the complementary channels cooperate live: inject
  a real Bot scenario (`POST /api/inject`) and the RandomForest flags it
  at 0.96+ confidence while the beaconing detector and T-GNN channels
  detect the metronomic timing regularity and topological drift. Three
  more feed the Hybrid IDS fusion layer
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
  deliberately **not** built on CII, providing an independent operational
  risk metric.
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

## Multi-Channel Detection Architecture & Evaluation

AEGIS is built on empirical rigor: rather than relying on a single fallible detector, AEGIS orchestrates **six specialized detection channels** fused through a mathematical Noisy-OR combinator:

| Detection Channel | Core Specialty | Key Metric | Mechanism |
|---|---|---|---|
| **Supervised Random Forest** | Known attack patterns | **P ≈ 0.996 (AUC 0.847)** | Pre-fitted on real labelled multi-day attack captures |
| **Deception Tripwire** | Pre-compromise early warning | **100% precision (0% FP)** | Zero-legitimate-use seeded credentials |
| **Beaconing Detector** | Periodic C2 & stealth implants | **Low CV threshold** | Inter-arrival timing coefficient of variation |
| **T-GNN (Topological)** | Structural & lateral anomalies | **Graph drift detection** | Sliding-window communication topology embeddings |
| **Signature Engine** | Protocol & metadata anomalies | **Deterministic rules** | Wire-speed flow header verification |
| **Isolation Forest** | High-volume traffic surges | **Calibrated outlier score** | Baseline volumetric density modeling |

### Key Engineering Insights & Design Choices

- **Multi-Channel Complementarity:** Supervised models excel on known attack distributions (achieving 0.996 precision and 0.847 AUC), while novel threat families require complementary structural and temporal signals. By fusing supervised learning, deception tripwires, protocol signatures, beaconing periodicity, and T-GNN graph topology, AEGIS provides defense-in-depth across the entire MITRE ATT&CK lifecycle.
- **Intelligent Alert Policy & Suppression:** In high-throughput industrial networks, raw volumetric noise can overwhelm SOC operators. AEGIS implements an active corroboration policy: raw volumetric fluctuations are calibrated, recorded, and suppressed from paging operators unless corroborated by independent channels or confirmed tripwires—drastically reducing alert fatigue.
- **Pre-Compromise Early Warning:** Network volume features are terminal aggregates (observed only after payloads transfer). The deception honeytoken channel provides an unambiguous pre-compromise signal during the attacker's initial reconnaissance phase, yielding up to **58.4 seconds of mean lead time** before physical asset compromise.
- **Tail-Risk Distribution Modeling:** For cascading impacts (CII), AEGIS avoids simplistic single-point estimates. Instead, it runs 1,000 Monte Carlo iterations in ~1.2 ms across the 50-node dependency graph, reporting full risk distributions from median to 95th percentile (p95) so operators can protect against worst-case cascading blackouts.


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
| `docs/DETECTION_STUDY.md` | empirical detector benchmarking and multi-channel study |
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
- **Soham Nangare** ([@sohamn06](https://github.com/sohamn06)) — Frontend & Systems Engineer
- **Parth Kakade** — Research & Detection Engineering
- **Samrudhi Divekar** — Systems & Data Engineering
- **Atharva Ambalge** — Infrastructure & Security Operations


