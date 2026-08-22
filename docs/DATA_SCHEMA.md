# Data & State Schema

AEGIS currently operates as a stateless backend engine that relies entirely on configuration files and CSV datasets rather than a traditional SQL/NoSQL database.

## 1. Graph State (The "Database")
Defined in `src/config.py`.
- **`SMART_CITY_ASSETS`**: Acts as the `Assets` table. Defines every node (IP, Type, Zone, Criticality).
- **`DEPENDENCY_GRAPH`**: Acts as the `Edges` table. Defines relationships (source, target, edge type, propagation probability).

## 2. Event Schema (`CanonicalEvent`)
Defined in `src/datasets/schema.py`.
All incoming datasets are mapped to this schema via Pydantic validators.

**Core Fields:**
- `timestamp`: Datetime of the event.
- `source_asset_id` / `destination_asset_id`: The edge where the event occurred.
- `action`: `ACTION_PASS` (Normal) or `ACTION_ALERT` (Anomaly/Attack).
- `raw_anomaly_score`: The uncalibrated score from the ML engine.
- `calibrated_score`: The sigmoid-calibrated probability (0 to 1) used by the CII engine.

## 3. Dynamic Features
Any non-canonical continuous numeric column is treated as a feature by the ML engine. For example, `duration_sec`, `packets`, and `bytes` for network traffic, or `FIT101`, `LIT101` for OT datasets like SWaT.
