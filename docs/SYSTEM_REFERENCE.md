# AEGIS System Reference

This document serves as the master guide to the AEGIS V2 project, directing you to the appropriate resources for various domains.

## Overview
AEGIS V2 is an anomaly detection and graph-based cascading impact analysis system. 

## Documentation Directory
All documentation is stored in the `/docs` folder:
- **[README.md](../README.md):** High-level overview and quick start.
- **[ARCHITECTURE.md](./ARCHITECTURE.md):** Details the 4-layer pipeline (Ingestion -> ML Engine -> CII Engine -> Frontend).
- **[DATA_SCHEMA.md](./DATA_SCHEMA.md):** Explains how datasets and graphs are represented in-memory.
- **[SETUP.md](./SETUP.md):** Installation, configuration, and execution instructions.
- **[DEVELOPMENT.md](./DEVELOPMENT.md):** Guidelines for adding datasets, testing, and linting.
- **[TECHNICAL_DEBT.md](./TECHNICAL_DEBT.md):** Known limitations and future improvement roadmaps.

## Key Source Files
- `src/datasets/loader.py`: The entry point for loading all datasets.
- `src/ml_engine.py`: Contains the `train_isolation_forest` and `compute_anomaly_scores` functions.
- `src/cii_calculator.py`: Contains the Monte Carlo simulation engine.
- `src/config.py`: The static graph database defining assets and dependencies.
- `src/settings.py`: The Pydantic configuration singleton.
- `src/evaluation/`: The CLI harness for benchmarking the system (`python -m evaluation`) — see `docs/EVALUATION.md`.
- `src/aegis_demo.py`: The interactive Streamlit dashboard.

## Testing
- Tests are located in the `/tests` directory and use `pytest`.
