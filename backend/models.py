"""
backend/models.py — SQLAlchemy 2.0 declarative models for the Phase 5 backend
(Ticket #2: schema + models + asset seeding).

Five tables: `assets` (curated topology only — see the module docstring
note on Decision D1 below), `events`, `event_scores`, `cii_snapshots`,
`alerts`. Created via `Base.metadata.create_all()` (backend/init_db.py) —
no Alembic (Decision D3: greenfield schema, no production data, 8-day
sprint).

Decision D1 — `events.source_asset` / `destination_asset` are plain text,
not foreign keys into `assets`. `AssetRegistry.resolve()` (src/datasets/
asset_registry.py, untouched by this ticket) mints one `Unresolved_<ip>`
node per unique IP it has never seen; real CIC-IDS2017 data carries
hundreds of unique IPs per 8k-row slice. A foreign key would force a write
into `assets` for every new IP on the ingest hot path, making the curated
topology table unbounded. `assets` stays the curated 16-node city topology
— "render assets, not packets" (docs/PHASE5_BUILD_PLAN.md section 5).

Decision D6 — `timing_provenance` is constrained with a plain `CHECK`
constraint against two module-level string constants, not a native
Postgres `ENUM` type. Rationale: per Opus decision P5-5, there are exactly
two currently-defined tiers (Monday's genuine capture-second timestamps vs.
the interpolated minute-bucket timing used Tue-Fri), and Postgres native
`ENUM` values can only be added via `ALTER TYPE ... ADD VALUE`, which is a
migration-shaped operation — the exact ceremony Decision D3 opted out of.
A `TEXT` column with a `CHECK` constraint is plain SQL, has no dialect-
specific catalog object to manage, and can be redefined by dropping and
re-adding the constraint if a third tier is ever introduced, without
touching the column type itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Reused, not redefined (CLAUDE.md section 5, "no duplicate definitions";
# ticket Decision D7). `backend/__init__.py` puts `src/` on sys.path so
# this import resolves to the canonical schema module.
from datasets.schema import VALID_SIGNAL_TYPES  # noqa: F401  (re-exported for callers)

# ---------------------------------------------------------------------------
# timing_provenance — module constants (Decision D6 / Opus decision P5-5)
# ---------------------------------------------------------------------------

TIMING_PROVENANCE_CAPTURE_SECONDS = "capture_seconds"
TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET = "interpolated_minute_bucket"

VALID_TIMING_PROVENANCE = {
    TIMING_PROVENANCE_CAPTURE_SECONDS,
    TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET,
}

# ---------------------------------------------------------------------------
# Asset "type" values for the two non-curated seed categories (Decision D2)
# ---------------------------------------------------------------------------

ASSET_TYPE_GATEWAY = "gateway"
ASSET_TYPE_SYNTHESIZED = "synthesized"


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# assets — curated city topology only (16 nodes), never grown on the ingest
# hot path. Seeded from graph_manager.build_graph() by backend/seed.py.
# ---------------------------------------------------------------------------


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str | None] = mapped_column(String, nullable=True)
    criticality: Mapped[float] = mapped_column(Float, nullable=False)
    purdue_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # server_default alongside the Python-side default: the Python default
    # only applies through the ORM, so a raw SQL INSERT (psql fixture, bulk
    # `INSERT ... SELECT`, manual inspection) would otherwise violate the
    # NOT NULL constraint (verified against a live DB — MEDIUM-1 review).
    is_gateway: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<Asset name={self.name!r} criticality={self.criticality} is_gateway={self.is_gateway}>"


# ---------------------------------------------------------------------------
# events — one row per ingested telemetry/financial/ICS/tripwire signal.
#
# Decision D5 — three distinct timestamps, all timestamptz:
#   ts           event time        (when the flow occurred, from the dataset)
#   observed_at  detection time    (C4 field; when the signal was observed/alerted)
#   ingested_at  processing time   (server-side default now())
# These must never be collapsed into one column.
#
# Decision D4 — de-duplication: (replay_session_id, source_row_id) is
# UNIQUE. This allows the exact same source CSV row to be replayed again in
# a fresh replay session (a new UUID) while rejecting a double-insert of
# the same row within one session (e.g. an ingest retry after a partial
# failure). Both columns are NOT NULL: every event is ingested within some
# replay/ingest session context, even a live (non-dataset-replay) source is
# expected to mint a session id, so the dedup invariant always applies.
# ---------------------------------------------------------------------------


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Decision D5: three distinct timestamps.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    destination_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Decision D1: plain text, not a foreign key into assets.
    source_asset: Mapped[str | None] = mapped_column(String, nullable=True)
    destination_asset: Mapped[str | None] = mapped_column(String, nullable=True)

    protocol: Mapped[str | None] = mapped_column(String, nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    packets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Values expected to be one of datasets.schema.VALID_SIGNAL_TYPES
    # (Decision D7) — not DB-enforced with a CHECK constraint because that
    # set is owned by src/datasets/schema.py and could grow; hardcoding it
    # into a DB constraint here would silently drift out of sync with the
    # canonical source instead of erroring loudly, which is worse.
    signal_type: Mapped[str] = mapped_column(String, nullable=False)

    source_dataset: Mapped[str | None] = mapped_column(String, nullable=True)

    # Decision D6: constrained via CHECK, see module docstring.
    timing_provenance: Mapped[str] = mapped_column(Text, nullable=False)

    # Decision D4: de-duplication pair, unique together.
    replay_session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_row_id: Mapped[str] = mapped_column(Text, nullable=False)

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "replay_session_id",
            "source_row_id",
            name="uq_events_replay_session_source_row",
        ),
        CheckConstraint(
            "timing_provenance IN ("
            f"'{TIMING_PROVENANCE_CAPTURE_SECONDS}', "
            f"'{TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET}'"
            ")",
            name="ck_events_timing_provenance",
        ),
        Index("ix_events_source_asset", "source_asset"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<Event id={self.id} ts={self.ts} signal_type={self.signal_type!r}>"


# Required index: events(ts DESC). Declared after the class body so
# `Event.ts` is a real InstrumentedAttribute with `.desc()` available
# (inside __table_args__, mapped_column() has not yet been resolved into
# a full column expression).
Index("ix_events_ts_desc", Event.ts.desc())


# ---------------------------------------------------------------------------
# event_scores — per-detector score for an event.
#
# ON DELETE CASCADE: a score row has no meaning without its parent event
# (it is purely derived from the event's feature vector), so when
# backend.retention.prune_events() deletes an old event, its scores should
# disappear with it rather than become orphaned rows that reference a
# nonexistent event_id.
# ---------------------------------------------------------------------------


class EventScore(Base):
    __tablename__ = "event_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    detector: Mapped[str] = mapped_column(String, nullable=False)
    raw_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibrated_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_event_scores_event_id", "event_id"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<EventScore event_id={self.event_id} detector={self.detector!r}>"


# ---------------------------------------------------------------------------
# cii_snapshots — a point-in-time Cascading Impact Index computation.
#
# ON DELETE SET NULL for trigger_event_id: a CII snapshot is an analytical
# record in its own right (median/p5/p95, impacted assets, hop details) —
# it has value independent of whether the raw event that triggered it is
# still on disk. If prune_events() deletes the triggering event, the
# snapshot survives with trigger_event_id set to NULL rather than being
# cascaded away (it is not "derived and worthless" the way an event_score
# row is) or blocking the prune (RESTRICT/NO ACTION would make old events
# referenced by a snapshot undeletable, defeating retention entirely).
# ---------------------------------------------------------------------------


class CiiSnapshot(Base):
    __tablename__ = "cii_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origin_asset: Mapped[str] = mapped_column(String, nullable=False)
    cii_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    cii_p5: Mapped[float | None] = mapped_column(Float, nullable=True)
    cii_p95: Mapped[float | None] = mapped_column(Float, nullable=True)
    impacted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    hop_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    trigger_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<CiiSnapshot id={self.id} origin_asset={self.origin_asset!r}>"


# ---------------------------------------------------------------------------
# alerts — operator-facing record, optionally backed by a CII snapshot.
#
# ON DELETE SET NULL for cii_snapshot_id, for the same reason as
# cii_snapshots.trigger_event_id above: an alert is the durable,
# acknowledgeable operator record. It must not disappear (CASCADE) or block
# retention (RESTRICT) just because the analytical snapshot it was created
# from eventually ages out; it survives with cii_snapshot_id set to NULL.
# Note this makes alerts two hops removed from events (alert ->
# cii_snapshot -> event), both hops SET NULL, so pruning old events never
# has a destructive or blocking effect on the alerts table.
# ---------------------------------------------------------------------------


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    asset: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cii_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("cii_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    # server_default alongside the Python-side default: see the matching
    # comment on Asset.is_gateway above (MEDIUM-1 review) — a raw SQL
    # INSERT that omits this column must not violate NOT NULL.
    acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<Alert id={self.id} severity={self.severity!r} acknowledged={self.acknowledged}>"


# Required composite index: alerts(acknowledged, ts DESC) — the "unacked,
# newest first" listing is the primary alerts-panel query.
Index("ix_alerts_acknowledged_ts_desc", Alert.acknowledged, Alert.ts.desc())
