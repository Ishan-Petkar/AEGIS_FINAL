# AEGIS: Anomalous Event Graph Intelligence System

AEGIS is a cyber-physical risk analytics platform for a simulated smart city
(municipal infrastructure + municipal financial systems). It combines
unsupervised anomaly detection with graph-based cascading impact analysis to
answer two questions: which telemetry events are anomalous, and if this
asset is compromised, what else falls over.

## Two consoles

- **Research Console** (`src/aegis_demo.py`, Streamlit) — the complete,
  working analytics engine: dataset selection, anomaly detection, the CII
  blast-radius calculator, scripted attack injection, and the evaluation/
  benchmark panel (honest precision/recall/F1/AUC and tripwire lead time
  against real ground truth). This is what you get running today.
- **Operations Console** (`backend/`, **Phase 5 — in progress**) — a live
  operations view built on the same engine: real CIC-IDS2017 traffic
  streamed in timestamp order, persisted to PostgreSQL, served through an
  API. **This is not yet a running product.** As of this writing the
  PostgreSQL schema, seeding, and a replay engine (real IP/timestamp
  extraction, chronological pacing, batched emission) exist and are tested;
  the FastAPI service, WebSocket streaming, the ingest pipeline that scores
  and persists events, and the Next.js frontend have **not** been built yet.
  See `PLAN_MASTER.md` §Phase 5 and `docs/PHASE5_STATE.md` for the current
  ticket-by-ticket status.

## Core capabilities

- **Unsupervised anomaly detection** — Isolation Forest (with Z-Score, MAD,
  and One-Class SVM baselines) over network-flow, financial-transaction, or
  ICS-sensor telemetry, no labeled training data required.
- **Mandatory gateway topology** — every path to a high-criticality asset is
  structurally rewritten to route through a Purdue-zone gateway node
  (`src/graph_manager.py`); this is a hard chokepoint in the graph itself,
  not a passive monitoring tap an attacker could bypass.
- **Honeytoken tripwire detection** — a fake credential seeded in each
  gateway zone (`src/deception/`); any use is unambiguous compromise by
  construction, with zero false positives and a measured lead-time
  advantage over volumetric detection (mean 58.4s across the four scripted
  attack scenarios).
- **Cascading Impact Index (CII)** — Monte Carlo simulation over a
  hand-curated asset dependency graph, reporting a **distribution**
  (median, p5, p95) of blast radius rather than a single point estimate.
- **Honest evaluation** (`src/evaluation/`) — segment-wise recall / row-wise
  precision for ICS time-series data (a deliberate, documented rejection of
  the "point-adjust" metric, which can make random noise look like a
  state-of-the-art detector), a guard that raises rather than silently
  reports zeros on a degenerate train/test split, and lead-time measured
  from real replayed attack timelines.

## Quick start — Research Console

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
streamlit run src/aegis_demo.py
```

Full instructions, including placing real datasets under `datasets/`:
`docs/SETUP.md`. Evaluation harness usage and metric definitions:
`docs/EVALUATION.md`.

## Quick start — Phase 5 backend (in progress)

The backend pieces that exist today can be exercised directly (schema init,
seeding, connectivity check, and the replay engine's own test suite); there
is no running API or UI yet.

```bash
pip install -r requirements-backend.txt
cp .env.example .env               # local-dev-only defaults; see docs/SETUP.md
python -m backend.init_db          # create tables + seed assets, idempotent
python -m backend.db_check         # verify connectivity
```

Full PostgreSQL setup (Homebrew install, role/database creation,
configuration): `docs/SETUP.md` §5. Architecture, ticket breakdown, and
design decisions: `PLAN_MASTER.md`.

## Documentation

- `PLAN_MASTER.md` — the authoritative plan: history, architecture
  decisions, phase-by-phase build record, and the active Phase 5 sprint plan.
- `docs/SETUP.md` — installation, including the PostgreSQL section for
  Phase 5.
- `docs/EVALUATION.md` — the evaluation protocol and how to reproduce
  published metrics.
- `docs/DATA_SCHEMA.md` — the canonical event schema and the Phase 5
  PostgreSQL schema.
- `docs/DEVELOPMENT.md` — how to add a dataset adapter, testing, linting,
  and the conventions CI enforces.
- `docs/DATASETS.md` — dataset sources and licences.
- `docs/DESIGN.md` — the dark-theme design tokens the dashboard CSS
  implements.

Treat the code as authoritative where a doc disagrees with it — this project
has gone through several phase migrations, and stale docs describing
superseded designs are a known, actively-tracked failure mode here (see
`PLAN_MASTER.md`'s own audit history). If you find another one, fix it.
