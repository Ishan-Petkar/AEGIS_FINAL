# Data & State Schema

AEGIS has two persistence layers today:

- **The engine (`src/`)** is still stateless — topology and edge probabilities
  live in `src/config.py`, and telemetry is read from CSV datasets or
  generated synthetically. This layer is unchanged by Phase 5 (see
  `PLAN_MASTER.md` Invariant A) and is what the Research Console
  (`src/aegis_demo.py`) runs against.
- **The Phase 5 backend (`backend/`)**, in progress, adds a **PostgreSQL**
  database for the Operations Console: continuous replay of real telemetry,
  scored and persisted, with alerts and CII snapshots an operator can query
  after the fact. This is new — earlier revisions of this document said AEGIS
  had no database; that is no longer accurate for the Phase 5 backend, though
  it remains true of the `src/` engine in isolation.

## 1. Config-Defined Topology (the engine's graph)

Defined in `src/config.py`:
- **`SMART_CITY_ASSETS`**: the curated city topology — every hand-defined
  node (IP, type, zone, criticality, `purdue_level`).
- **`DEPENDENCY_GRAPH`**: edges between assets (source, target, edge type,
  propagation probability, plus provenance metadata).

`SMART_CITY_ASSETS` is not the complete graph, though. `src/graph_manager.py`'s
`build_graph()` is the sole graph constructor (contract C2), and it
**synthesizes gateway nodes** (`Gateway_L<purdue-level>`) that do not appear
in `SMART_CITY_ASSETS` at all: any inbound edge to an asset at or above the
protection-criticality threshold is rewritten to route through its
Purdue-zone gateway instead of terminating on the asset directly. Gateway
nodes carry near-zero criticality by design (a hit on the gateway is pure
detection signal, not blast-radius mass) and exist purely in the graph object
`build_graph()` returns — there is no separate config list of them. The
Phase 5 `assets` table (below) seeds from `build_graph()`, not from
`SMART_CITY_ASSETS` alone, for the same reason: it needs the gateway nodes
too. A third category, `synthesized`, covers nodes that appear only as
`DEPENDENCY_GRAPH` edge targets (currently just `City_Grid`) and were never
declared as a curated asset.

## 2. Canonical Event Schema (`CanonicalEvent`, v2.0)

Defined in `src/datasets/schema.py`. Every ingestion path (CIC-IDS2017,
PaySim, SWaT, the synthetic generator, the Phase 2 deception adapter, and the
Phase 5 replay path) normalises into this schema before any downstream
component touches it — no component may read a dataset-specific column
directly.

The canonical columns, in schema order (`CANONICAL_COLUMNS`):

| Field | Type | Notes |
|---|---|---|
| `timestamp` | datetime | When the event occurred in the source system. |
| `source_asset_id` | str | Resolved asset name or raw identifier (IP, account ID). |
| `destination_asset_id` | str | Resolved asset name or raw identifier. |
| `protocol` | str | Network protocol or transaction type. |
| `payload_size` | float ≥ 0 | Bytes transferred (network) or transaction amount (financial). |
| `action` | str | `ACTION_PASS` / `ACTION_ALERT` / `ACTION_DENY` / `ACTION_PAYMENT` / `ACTION_CASH_OUT` / `ACTION_CASH_IN` / `ACTION_DEBIT` / `ACTION_TRANSFER`. `action == ACTION_ALERT` is the ground truth an evaluation run derives `y_true` from. |
| `zone` | str | Network/operational zone (`INTERNAL`, `DMZ`, `FINANCIAL_EXTERNAL`, `ICS`, …). |
| `process_or_service` | str | Service, protocol, or business process responsible for the event. |
| `attck_evidence` | str \| None | MITRE ATT&CK technique or fraud type. `None` if benign. |
| `raw_anomaly_score` | float \| None, [0,1] | Uncalibrated ML score. Null at ingestion, populated after scoring. |
| `calibrated_alert_level` | str \| None | Conformal-calibrated severity: `CRITICAL`/`HIGH`/`MEDIUM`/`LOW`/`INFO`. **This is the actual field name** — there is no separate `calibrated_score` column in the canonical schema (an earlier revision of this document used that name; it does not exist). The numeric sigmoid-calibrated probability that field name might suggest is a `core.pipeline` / `ml_engine` computation, not a schema column. |
| `provenance` | str | Dataset/source identifier — one of the `PROVENANCE_*` constants. |
| `confidence` | float, [0,1] | Asset-resolution confidence (1.0 = exact match, lower = inferred). |
| `schema_version` | str | Currently `"2.0"`. |
| `signal_type` | str | **v2.0 addition (contract C4).** One of `network_flow`, `financial_txn`, `ics_reading`, `deception_tripwire`. Determines which feature-extraction path `to_ml_features()` uses — a `deception_tripwire` event has no real flow volume, so its `duration_sec`/`packets`/`bytes` are zeroed rather than fabricated from `payload_size`. Fabricating them would reintroduce the circular-labeling bug the schema migration exists to prevent (see `PLAN_MASTER.md` §1.3). |
| `observed_at` | datetime \| None | **v2.0 addition (contract C4).** Wall-clock time AEGIS detected/observed the event, distinct from `timestamp` (when the event occurred). `None` means no detection latency is modeled — treat as equal to `timestamp`. This is the field the Phase 3 lead-time metric is computed from. |
| `purdue_level` | int \| None, 0–5 | **v2.0 addition (contract C4).** ISA-95/Purdue level of the originating asset (0 = physical process/sensors … 5 = enterprise/external). `None` if not yet classified. |

Adapters may attach additional non-canonical columns (`duration_sec`,
`packets`, `bytes` for network-shaped features, or dataset-specific sensor
columns like SWaT's `FIT101`/`LIT101`) as ML features.
`CanonicalBatch.validate_schema()` only checks that required columns are
**present**, so extra columns pass through untouched — see
`CanonicalBatch.to_ml_features()` for how they're consumed.

## 3. PostgreSQL Schema (Phase 5, `backend/models.py`)

Five tables, created via SQLAlchemy `Base.metadata.create_all()` (no Alembic
— a deliberate choice for a greenfield schema with no production data to
migrate). Seeded and initialized with `python -m backend.init_db`.

### `assets`

The curated topology, seeded from `graph_manager.build_graph()` (not grown on
the ingest hot path — see Decision D1 below). One row per node: `name`
(unique), `ip`, `type` (`curated` implied by absence of the other two types,
or explicitly `gateway` / `synthesized`), `criticality`, `purdue_level`,
`is_gateway`.

### `events`

One row per ingested telemetry/financial/ICS/tripwire signal. Notable
columns: `source_asset` / `destination_asset` (plain text, not foreign keys
— see below), `signal_type`, `source_dataset`, `timing_provenance`, and a
`raw` JSONB column holding the original row.

**Three distinct timestamps**, all `timestamptz`, that must never be
collapsed into one column:

| Column | Meaning |
|---|---|
| `ts` | **Event time** — when the flow occurred, taken from the dataset. |
| `observed_at` | **Detection time** — when the signal was observed/alerted (the C4 field). |
| `ingested_at` | **Processing time** — server-side `now()` at insert. |

Collapsing these would conflate "when it happened" with "when we noticed"
with "when our pipeline got around to writing it down" — exactly the kind of
conflation that made lead-time claims unmeasurable before Phase 3.

**Key constraints:**
- `UNIQUE (replay_session_id, source_row_id)` — lets the exact same source
  CSV row be replayed again in a fresh replay session (new UUID) while
  rejecting a double-insert of the same row within one session (e.g. a retry
  after a partial ingest failure).
- `CHECK (timing_provenance IN ('capture_seconds', 'interpolated_minute_bucket'))`
  — see the `timing_provenance` section below.
- Indexes: `ix_events_source_asset`, `ix_events_ts_desc` (`ts DESC`, needed
  because the live-feed query is "most recent events").

### `event_scores`

Per-detector score for an event: `detector`, `raw_score`, `calibrated_score`,
`is_anomaly`, `confidence`. `event_id` is a foreign key to `events.id` with
**`ON DELETE CASCADE`** — a score has no meaning independent of its event (it
is purely derived from the event's feature vector), so pruning an old event
should take its scores with it rather than leaving orphaned rows.

### `cii_snapshots`

A point-in-time Cascading Impact Index computation: `origin_asset`,
`cii_median`/`cii_p5`/`cii_p95`, `impacted` (JSONB), `hop_details` (JSONB),
and `trigger_event_id` — a foreign key to `events.id` with **`ON DELETE SET
NULL`**. A CII snapshot is an analytical record with value independent of
whether the triggering event is still on disk; if the event is pruned, the
snapshot survives with `trigger_event_id` set to `NULL` rather than being
deleted (`CASCADE` would treat it as disposable, which it isn't) or blocking
the prune (`RESTRICT` would make old events undeletable, defeating retention).

### `alerts`

The operator-facing record: `severity`, `asset`, `title`, `detail`,
`explanation` (JSONB — the per-feature deviation from `StreamingScorer.explain()`),
`acknowledged`, `acknowledged_at`, and `cii_snapshot_id` — a foreign key to
`cii_snapshots.id`, also **`ON DELETE SET NULL`**, for the same reasoning as
above: an alert is the durable, acknowledgeable record and must not disappear
or block retention just because the CII snapshot it referenced eventually
ages out. This makes `alerts` two hops removed from `events`
(`alert → cii_snapshot → event`), both hops `SET NULL`, so pruning old events
never has a destructive or blocking effect on the alerts table.

**Why `assets.source_asset`/`destination_asset` are plain text, not foreign
keys (Decision D1):** `AssetRegistry.resolve()` mints one `Unresolved_<ip>`
node per unique IP it has never seen, and real CIC-IDS2017 data carries
hundreds of unique IPs per 8k-row slice. A foreign key into `assets` would
force a write into the curated topology table for every new IP on the ingest
hot path, making a table that's supposed to stay a curated ~16-node city
topology grow unbounded. "Render assets, not packets" — `assets` stays
curated; raw identity resolution happens in the ingest layer, not the schema.

## 4. `timing_provenance`

CIC-IDS2017's `TrafficLabelling` CSVs (the source for Phase 5 replay, distinct
from the `MachineLearningCVE` CSVs the Research Console's `CICIDSAdapter`
uses) have inconsistent timestamp precision across capture days: Monday has
genuine second-level capture timestamps; Tuesday–Friday only resolve to the
minute. Every replayed event is tagged with one of two `timing_provenance`
values:

- **`capture_seconds`** — the timestamp is a real, second-resolution capture
  time (Monday only).
- **`interpolated_minute_bucket`** — the source only resolved to the minute;
  the replay engine distributes that minute's events evenly across the
  interval to pace a smooth stream, preserving real relative volume (a
  genuinely busy minute still emits proportionally more events), but the
  sub-minute ordering and exact instant **are synthetic**.

This is not stored as a native Postgres `ENUM` but as a `TEXT` column with a
`CHECK` constraint — there are exactly two tiers today, and a `CHECK` can be
dropped and re-added without a migration-shaped `ALTER TYPE ... ADD VALUE` if
a third tier is ever introduced.

The load-bearing rule: **interpolated timestamps are never presented as
original arrival times.** The interpolated virtual time used for replay
pacing is computed separately (`compute_virtual_times()` in the replay
reader) and is never written back into the event's `ts` column or exposed to
the UI as an observed instant — `timing_provenance` is the sole honest record
of which tier a row came from, and any consumer of `ts` on an
`interpolated_minute_bucket` row must treat it as minute-accurate, not
second-accurate.
