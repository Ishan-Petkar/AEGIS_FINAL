# AEGIS System Reference

This document serves as the master guide to the AEGIS project, directing you to the appropriate resources for various domains.

> **Updated 2026-09-02** (Phase A improvement pass). The previous revision
> of this file predated Phase 5 entirely — it linked to a deleted
> `ARCHITECTURE.md` and a `TECHNICAL_DEBT.md` that never existed, and
> described only the pre-Phase-5 Research Console. Per this project's own
> stated policy ("code is authoritative where docs disagree; fix stale
> docs when you find them"), it's rewritten below to match the current
> two-console architecture.

## Overview
AEGIS is a cyber-physical risk analytics platform for a simulated smart
city, running as **two consoles sharing one detection/CII engine**:

- **Research Console** (`src/aegis_demo.py`, Streamlit) — the analytics
  workbench: dataset selection, detector benchmarking, and the evaluation
  harness.
- **Operations Console** (`backend/` + `frontend/`) — the live product:
  real captured traffic replayed in chronological order, scored, persisted
  to PostgreSQL, streamed over a WebSocket, and rendered in a Next.js
  console.

## Documentation Directory
All documentation is stored in the repo root and `/docs` (see also
`README.md`'s own "Documentation" table, which this mirrors):
- **[README.md](../README.md):** High-level overview, architecture diagram, quick start.
- **[CLAUDE.md](../CLAUDE.md):** Guidance for AI coding agents working in this repo — conventions, known issues, invariants.
- **[PLAN_MASTER.md](../PLAN_MASTER.md):** Authoritative plan, architecture decisions, phase-by-phase history.
- **[PHASE5_STATE.md](./PHASE5_STATE.md):** Ticket-by-ticket Phase 5 build record and known issues.
- **[DETECTION_STUDY.md](./DETECTION_STUDY.md):** The detector measurements behind README's "Honest limitations" section.
- **[EVALUATION.md](./EVALUATION.md):** Evaluation protocol and how to reproduce the metrics.
- **[DATA_SCHEMA.md](./DATA_SCHEMA.md):** The canonical event schema and the PostgreSQL schema.
- **[DATASETS.md](./DATASETS.md):** Dataset sources and licences.
- **[SETUP.md](./SETUP.md):** Installation, including PostgreSQL.
- **[DEVELOPMENT.md](./DEVELOPMENT.md):** Guidelines for adding datasets, testing, and linting.
- **[DESIGN_CONSOLE.md](./DESIGN_CONSOLE.md):** Operations Console design tokens ("Warm Industrial Glass").
- **[DESIGN.md](./DESIGN.md):** Research Console (Streamlit) design tokens.

## Key Source Files

**Shared engine** (`src/`, imported by both consoles):
- `src/datasets/loader.py`: the sole entry point for loading all datasets — never call an adapter directly.
- `src/ml_engine.py`: `train_isolation_forest` / `compute_anomaly_scores`.
- `src/cii_calculator.py`: the Monte Carlo cascading-impact simulation engine.
- `src/graph_manager.py`: the sole graph builder — also where the mandatory Purdue-zone gateway rewrite lives.
- `src/config.py`: the static asset/dependency-graph data (`SMART_CITY_ASSETS`, `DEPENDENCY_GRAPH`).
- `src/settings.py`: the Pydantic configuration singleton (`SETTINGS`).
- `src/evaluation/`: the CLI benchmarking harness (`python -m evaluation`) — see `docs/EVALUATION.md`.

**Research Console:**
- `src/aegis_demo.py`: the interactive Streamlit dashboard.

**Operations Console:**
- `backend/main.py`, `backend/routes.py`: the FastAPI app and its 12 REST routes + 1 WebSocket.
- `backend/replay_engine.py`, `backend/replay_reader.py`: chronological, speed-controlled replay of real captured traffic.
- `backend/ingest.py`: score → resolve → persist → CII → alert → broadcast, per micro-batch.
- `backend/inject.py`: real, re-targeted attack-flow injection ("what-if" scenarios).
- `backend/ws_broadcaster.py`: the WebSocket fan-out layer.
- `backend/models.py`: the SQLAlchemy schema (`assets`, `events`, `event_scores`, `cii_snapshots`, `alerts`).
- `frontend/src/app/page.tsx`: the console's top-level layout (header, telemetry rail, city graph, alerts rail).
- `frontend/src/lib/useEventStream.ts`, `frontend/src/lib/stream-context.tsx`: the single shared WebSocket connection.

## Testing
- Tests are located in the `/tests` directory and use `pytest`.
- Run with `PYTHONPATH=src python -m pytest tests/ -q` from the repo root (bare `pytest` without `PYTHONPATH=src` will fail to import `backend` — see `tests/conftest.py`).
