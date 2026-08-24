"""
tests/test_backend_models.py — Phase 5 Ticket #2: schema, models, seeding,
retention.

The default suite here touches NO database (CI has no Postgres): it
exercises SQLAlchemy table metadata (columns, nullability, constraints,
indexes, FK targets and ON DELETE behaviour) built purely from
backend/models.py, plus the pure seeding/retention logic functions
(backend.seed.compute_seed_rows, backend.retention._rows_to_prune).

Live-DB tests (create tables -> seed -> assert -> prune -> drop) are gated
behind AEGIS_TEST_LIVE_DB=1 and skipped by default.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.models import (
    ASSET_TYPE_GATEWAY,
    ASSET_TYPE_SYNTHESIZED,
    TIMING_PROVENANCE_CAPTURE_SECONDS,
    TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET,
    VALID_TIMING_PROVENANCE,
    Alert,
    Asset,
    Base,
    CiiSnapshot,
    Event,
    EventScore,
)
from backend.retention import _rows_to_prune
from backend.seed import compute_seed_rows

# ---------------------------------------------------------------------------
# Table presence
# ---------------------------------------------------------------------------


def test_all_five_tables_registered():
    assert set(Base.metadata.tables.keys()) == {
        "assets",
        "events",
        "event_scores",
        "cii_snapshots",
        "alerts",
    }


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------


def test_assets_columns_present_and_nullability():
    cols = Base.metadata.tables["assets"].columns
    assert {"id", "name", "ip", "type", "criticality", "purdue_level", "is_gateway"} <= set(
        cols.keys()
    )
    assert cols["name"].nullable is False
    assert cols["name"].unique is True
    assert cols["criticality"].nullable is False
    assert cols["is_gateway"].nullable is False
    assert cols["ip"].nullable is True
    assert cols["purdue_level"].nullable is True


# ---------------------------------------------------------------------------
# events — three timestamps (D5), dedup pair (D4), timing_provenance (D6)
# ---------------------------------------------------------------------------


def test_events_columns_present():
    cols = set(Base.metadata.tables["events"].columns.keys())
    expected = {
        "id",
        "ts",
        "observed_at",
        "ingested_at",
        "source_id",
        "destination_id",
        "source_asset",
        "destination_asset",
        "protocol",
        "bytes",
        "packets",
        "duration_sec",
        "signal_type",
        "source_dataset",
        "timing_provenance",
        "replay_session_id",
        "source_row_id",
        "raw",
    }
    assert expected <= cols


def test_events_three_timestamps_are_distinct_and_timezone_aware():
    cols = Base.metadata.tables["events"].columns
    for name in ("ts", "observed_at", "ingested_at"):
        assert cols[name].type.timezone is True, f"{name} must be timestamptz"
    # D5: must not be collapsed - three distinct column objects.
    assert cols["ts"] is not cols["observed_at"]
    assert cols["ts"] is not cols["ingested_at"]
    assert cols["observed_at"] is not cols["ingested_at"]


def test_events_ts_not_null_but_observed_at_nullable():
    cols = Base.metadata.tables["events"].columns
    assert cols["ts"].nullable is False
    assert cols["observed_at"].nullable is True
    assert cols["ingested_at"].nullable is False
    assert cols["ingested_at"].server_default is not None


def test_events_signal_type_and_timing_provenance_not_null():
    cols = Base.metadata.tables["events"].columns
    assert cols["signal_type"].nullable is False
    assert cols["timing_provenance"].nullable is False


def test_events_dedup_columns_not_null():
    cols = Base.metadata.tables["events"].columns
    assert cols["replay_session_id"].nullable is False
    assert cols["source_row_id"].nullable is False


def test_events_dedup_unique_constraint_on_the_pair():
    table = Base.metadata.tables["events"]
    unique_constraints = [
        c for c in table.constraints if type(c).__name__ == "UniqueConstraint"
    ]
    assert len(unique_constraints) == 1
    assert {col.name for col in unique_constraints[0].columns} == {
        "replay_session_id",
        "source_row_id",
    }


def test_events_ts_desc_index_exists():
    table = Base.metadata.tables["events"]
    names = {ix.name for ix in table.indexes}
    assert "ix_events_ts_desc" in names


def test_events_source_asset_index_exists():
    table = Base.metadata.tables["events"]
    matching = [ix for ix in table.indexes if ix.name == "ix_events_source_asset"]
    assert len(matching) == 1
    assert {c.name for c in matching[0].columns} == {"source_asset"}


def test_events_source_asset_and_destination_asset_are_plain_text_not_fk():
    """Decision D1: no foreign key into assets from events."""
    cols = Base.metadata.tables["events"].columns
    assert cols["source_asset"].foreign_keys == set()
    assert cols["destination_asset"].foreign_keys == set()


# ---------------------------------------------------------------------------
# timing_provenance allowed values (D6)
# ---------------------------------------------------------------------------


def test_timing_provenance_constants_are_exactly_two():
    assert VALID_TIMING_PROVENANCE == {
        "capture_seconds",
        "interpolated_minute_bucket",
    }
    assert TIMING_PROVENANCE_CAPTURE_SECONDS == "capture_seconds"
    assert TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET == "interpolated_minute_bucket"


def test_timing_provenance_check_constraint_references_both_values():
    table = Base.metadata.tables["events"]
    checks = [c for c in table.constraints if type(c).__name__ == "CheckConstraint"]
    assert len(checks) == 1
    sql_text = str(checks[0].sqltext)
    assert TIMING_PROVENANCE_CAPTURE_SECONDS in sql_text
    assert TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET in sql_text


# ---------------------------------------------------------------------------
# event_scores
# ---------------------------------------------------------------------------


def test_event_scores_columns_and_fk():
    cols = Base.metadata.tables["event_scores"].columns
    assert {
        "id",
        "event_id",
        "detector",
        "raw_score",
        "calibrated_score",
        "is_anomaly",
        "confidence",
    } <= set(cols.keys())
    fks = list(cols["event_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "events"
    assert fks[0].ondelete == "CASCADE"
    assert cols["event_id"].nullable is False
    assert cols["is_anomaly"].nullable is False


def test_event_scores_event_id_index_exists():
    table = Base.metadata.tables["event_scores"]
    names = {ix.name for ix in table.indexes}
    assert "ix_event_scores_event_id" in names


# ---------------------------------------------------------------------------
# cii_snapshots
# ---------------------------------------------------------------------------


def test_cii_snapshots_columns_and_fk_set_null():
    cols = Base.metadata.tables["cii_snapshots"].columns
    assert {
        "id",
        "ts",
        "origin_asset",
        "cii_median",
        "cii_p5",
        "cii_p95",
        "impacted",
        "hop_details",
        "trigger_event_id",
    } <= set(cols.keys())
    fks = list(cols["trigger_event_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "events"
    assert fks[0].ondelete == "SET NULL"
    # SET NULL requires a nullable column - enforced here, not left to DB default.
    assert cols["trigger_event_id"].nullable is True


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------


def test_alerts_columns_and_fk_set_null():
    cols = Base.metadata.tables["alerts"].columns
    assert {
        "id",
        "ts",
        "severity",
        "asset",
        "title",
        "detail",
        "explanation",
        "cii_snapshot_id",
        "acknowledged",
        "acknowledged_at",
    } <= set(cols.keys())
    fks = list(cols["cii_snapshot_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "cii_snapshots"
    assert fks[0].ondelete == "SET NULL"
    assert cols["cii_snapshot_id"].nullable is True


def test_alerts_acknowledged_not_null_default_false():
    cols = Base.metadata.tables["alerts"].columns
    assert cols["acknowledged"].nullable is False
    # default=False is a Python-side default (mapped_column default=)
    assert cols["acknowledged"].default is not None
    assert cols["acknowledged"].default.arg is False


def test_alerts_acknowledged_has_db_server_default_false():
    """MEDIUM-1 review fix: a raw SQL INSERT (not going through the ORM)
    must not violate the NOT NULL constraint on this column."""
    cols = Base.metadata.tables["alerts"].columns
    assert cols["acknowledged"].server_default is not None
    assert "false" in str(cols["acknowledged"].server_default.arg).lower()


def test_assets_is_gateway_has_db_server_default_false():
    """MEDIUM-1 review fix: same as alerts.acknowledged above."""
    cols = Base.metadata.tables["assets"].columns
    assert cols["is_gateway"].server_default is not None
    assert "false" in str(cols["is_gateway"].server_default.arg).lower()


def test_alerts_composite_index_acknowledged_ts_desc():
    table = Base.metadata.tables["alerts"]
    matching = [ix for ix in table.indexes if ix.name == "ix_alerts_acknowledged_ts_desc"]
    assert len(matching) == 1
    assert {c.name for c in matching[0].columns} == {"acknowledged", "ts"}


# ---------------------------------------------------------------------------
# Seeding logic (backend/seed.py) — pure, no DB
# ---------------------------------------------------------------------------


def test_seed_rows_cover_all_16_graph_nodes():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from graph_manager import build_graph  # noqa: E402

    rows = compute_seed_rows()
    graph_nodes = set(build_graph(directed=True).nodes())
    assert len(rows) == 16
    assert {r["name"] for r in rows} == graph_nodes


def test_seed_rows_no_duplicate_names():
    rows = compute_seed_rows()
    names = [r["name"] for r in rows]
    assert len(names) == len(set(names))


def test_gateways_get_zero_criticality_and_is_gateway_true():
    rows = {r["name"]: r for r in compute_seed_rows()}
    for gw_name, expected_purdue in (
        ("Gateway_L1", 1),
        ("Gateway_L3", 3),
        ("Gateway_L4", 4),
        ("Gateway_L5", 5),
    ):
        assert gw_name in rows, f"{gw_name} missing from seed rows"
        row = rows[gw_name]
        assert row["criticality"] == 0.0
        assert row["is_gateway"] is True
        assert row["ip"] is None
        assert row["type"] == ASSET_TYPE_GATEWAY
        assert row["purdue_level"] == expected_purdue


def test_exactly_four_gateways_seeded():
    rows = compute_seed_rows()
    gateway_rows = [r for r in rows if r["is_gateway"]]
    assert {r["name"] for r in gateway_rows} == {
        "Gateway_L1",
        "Gateway_L3",
        "Gateway_L4",
        "Gateway_L5",
    }


def test_city_grid_gets_synthesized_defaults():
    rows = {r["name"]: r for r in compute_seed_rows()}
    assert "City_Grid" in rows
    row = rows["City_Grid"]
    assert row["criticality"] == 0.5
    assert row["ip"] is None
    assert row["type"] == ASSET_TYPE_SYNTHESIZED
    assert row["purdue_level"] is None
    assert row["is_gateway"] is False


def test_curated_assets_keep_real_config_values():
    rows = {r["name"]: r for r in compute_seed_rows()}
    curated_expected = {
        "Traffic_Cam_1": {"ip": "10.0.1.10", "type": "IoT Sensor", "criticality": 0.2, "purdue_level": 0},
        "Power_Substation": {"ip": "10.0.1.13", "type": "Critical Infra", "criticality": 1.0, "purdue_level": 1},
        "City_Payment_Gateway": {
            "ip": "10.0.1.20",
            "type": "Financial Transaction System",
            "criticality": 0.95,
            "purdue_level": 4,
        },
        "Bank_Partner_API": {
            "ip": "10.0.1.21",
            "type": "External Financial Interface",
            "criticality": 0.85,
            "purdue_level": 5,
        },
    }
    for name, expected in curated_expected.items():
        assert name in rows, f"{name} missing from seed rows"
        row = rows[name]
        assert row["ip"] == expected["ip"]
        assert row["type"] == expected["type"]
        assert row["criticality"] == expected["criticality"]
        assert row["purdue_level"] == expected["purdue_level"]
        assert row["is_gateway"] is False


def test_eleven_curated_assets_match_smart_city_assets():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from config import SMART_CITY_ASSETS  # noqa: E402

    rows = {r["name"]: r for r in compute_seed_rows()}
    curated_names = {a["asset_name"] for a in SMART_CITY_ASSETS}
    assert len(curated_names) == 11
    for name in curated_names:
        assert rows[name]["is_gateway"] is False
        assert rows[name]["type"] != ASSET_TYPE_GATEWAY
        assert rows[name]["type"] != ASSET_TYPE_SYNTHESIZED


# ---------------------------------------------------------------------------
# Retention logic (backend/retention.py) — pure, no DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "total_count,max_rows,expected",
    [
        (0, 500_000, 0),
        (400_000, 500_000, 0),
        (500_000, 500_000, 0),
        (600_000, 500_000, 100_000),
        (1000, 1000, 0),
        (1001, 1000, 1),
    ],
)
def test_rows_to_prune_arithmetic(total_count, max_rows, expected):
    assert _rows_to_prune(total_count, max_rows) == expected


def test_rows_to_prune_never_negative():
    assert _rows_to_prune(total_count=10, max_rows=1_000_000) == 0


# ---------------------------------------------------------------------------
# Live-DB tests — opt-in only (AEGIS_TEST_LIVE_DB=1), skipped by default.
# ---------------------------------------------------------------------------

_LIVE_DB = os.environ.get("AEGIS_TEST_LIVE_DB") == "1"

pytestmark_live = pytest.mark.skipif(
    not _LIVE_DB, reason="Live-DB tests are opt-in: set AEGIS_TEST_LIVE_DB=1 (requires local Postgres)"
)


@pytest.fixture(scope="module")
def live_engine():
    if not _LIVE_DB:
        pytest.skip("AEGIS_TEST_LIVE_DB not set")
    from backend.db import get_engine

    engine = get_engine()
    Base.metadata.create_all(engine)
    yield engine
    # Leave tables in place (init_db.py verification runs share this DB);
    # only the rows this test module itself created are cleaned up by the
    # per-test fixtures below.


@pytest.fixture()
def live_session(live_engine):
    from backend.db import get_session_factory

    session = get_session_factory()()
    yield session
    session.rollback()
    session.close()


@pytestmark_live
def test_live_create_tables_and_seed_assets(live_session):
    from backend.seed import seed_assets

    result = seed_assets(live_session)
    live_session.commit()
    assert result["total"] == 16

    count = live_session.query(Asset).count()
    assert count == 16

    gateways = live_session.query(Asset).filter(Asset.is_gateway.is_(True)).all()
    assert len(gateways) == 4
    for gw in gateways:
        assert gw.criticality == 0.0

    # Idempotency: seeding again creates/updates nothing.
    result2 = seed_assets(live_session)
    live_session.commit()
    assert result2["created"] == 0
    assert result2["updated"] == 0
    assert live_session.query(Asset).count() == 16


@pytestmark_live
def test_live_alerts_acknowledged_raw_insert_uses_db_default(live_session):
    """MEDIUM-1 verification: a raw SQL INSERT that omits `acknowledged`
    must succeed and store `false`, proving the DB-level default (not just
    the ORM-side Python default) is in place."""
    from sqlalchemy import text

    live_session.execute(
        text(
            "INSERT INTO alerts (ts, severity, asset, title) "
            "VALUES (now(), 'HIGH', 'City_Payment_Gateway', '__test_raw_insert_alert__')"
        )
    )
    live_session.commit()

    row = live_session.execute(
        text("SELECT acknowledged FROM alerts WHERE title = '__test_raw_insert_alert__'")
    ).first()
    assert row is not None
    assert row[0] is False

    live_session.execute(text("DELETE FROM alerts WHERE title = '__test_raw_insert_alert__'"))
    live_session.commit()


@pytestmark_live
def test_live_assets_is_gateway_raw_insert_uses_db_default(live_session):
    """MEDIUM-1 verification: same as the alerts.acknowledged test above,
    for assets.is_gateway."""
    from sqlalchemy import text

    live_session.execute(
        text("INSERT INTO assets (name, criticality) VALUES ('__test_raw_insert_asset__', 0.1)")
    )
    live_session.commit()

    row = live_session.execute(
        text("SELECT is_gateway FROM assets WHERE name = '__test_raw_insert_asset__'")
    ).first()
    assert row is not None
    assert row[0] is False

    live_session.execute(text("DELETE FROM assets WHERE name = '__test_raw_insert_asset__'"))
    live_session.commit()


@pytestmark_live
def test_live_seed_assets_reports_stale_without_deleting(live_session):
    """MEDIUM-2 verification: an assets row whose name is not among the
    current compute_seed_rows() names must be reported (stale count +
    stale_names) by seed_assets(), and must NOT be deleted."""
    from sqlalchemy import text

    from backend.seed import seed_assets

    live_session.execute(
        text("INSERT INTO assets (name, criticality) VALUES ('__bogus_stale_asset__', 0.42)")
    )
    live_session.commit()

    result = seed_assets(live_session)
    live_session.commit()

    assert result["stale"] >= 1
    assert "__bogus_stale_asset__" in result["stale_names"]

    still_present = (
        live_session.query(Asset).filter(Asset.name == "__bogus_stale_asset__").first()
    )
    assert still_present is not None

    live_session.delete(still_present)
    live_session.commit()


@pytestmark_live
def test_live_events_dedup_unique_constraint(live_session):
    from sqlalchemy.exc import IntegrityError

    session_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    e1 = Event(
        ts=now,
        source_asset="Traffic_Cam_1",
        destination_asset="Traffic_Controller",
        signal_type="network_flow",
        timing_provenance=TIMING_PROVENANCE_CAPTURE_SECONDS,
        replay_session_id=session_id,
        source_row_id="row-1",
    )
    live_session.add(e1)
    live_session.commit()

    dup = Event(
        ts=now,
        source_asset="Traffic_Cam_1",
        destination_asset="Traffic_Controller",
        signal_type="network_flow",
        timing_provenance=TIMING_PROVENANCE_CAPTURE_SECONDS,
        replay_session_id=session_id,
        source_row_id="row-1",
    )
    live_session.add(dup)
    with pytest.raises(IntegrityError):
        live_session.commit()
    live_session.rollback()

    # Same source_row_id but a *different* replay session is allowed (a
    # fresh replay of the same source data).
    replay_again = Event(
        ts=now,
        source_asset="Traffic_Cam_1",
        destination_asset="Traffic_Controller",
        signal_type="network_flow",
        timing_provenance=TIMING_PROVENANCE_CAPTURE_SECONDS,
        replay_session_id=uuid.uuid4(),
        source_row_id="row-1",
    )
    live_session.add(replay_again)
    live_session.commit()

    live_session.query(Event).filter(Event.replay_session_id.in_([session_id, replay_again.replay_session_id])).delete(
        synchronize_session=False
    )
    live_session.commit()


@pytestmark_live
def test_live_timing_provenance_check_constraint_rejects_bad_value(live_session):
    from sqlalchemy.exc import IntegrityError

    bad = Event(
        ts=datetime.now(timezone.utc),
        signal_type="network_flow",
        timing_provenance="not_a_real_value",
        replay_session_id=uuid.uuid4(),
        source_row_id="row-bad",
    )
    live_session.add(bad)
    with pytest.raises(IntegrityError):
        live_session.commit()
    live_session.rollback()


@pytestmark_live
def test_live_event_scores_cascade_on_event_delete(live_session):
    session_id = uuid.uuid4()
    event = Event(
        ts=datetime.now(timezone.utc),
        signal_type="network_flow",
        timing_provenance=TIMING_PROVENANCE_CAPTURE_SECONDS,
        replay_session_id=session_id,
        source_row_id="row-cascade",
    )
    live_session.add(event)
    live_session.commit()

    score = EventScore(event_id=event.id, detector="isolation_forest", raw_score=-0.1, is_anomaly=True)
    live_session.add(score)
    live_session.commit()
    score_id = score.id

    live_session.delete(event)
    live_session.commit()
    # The DB-level ON DELETE CASCADE deleted `score` as a side effect, not
    # the ORM - expire the identity map so get() re-queries instead of
    # returning the stale in-memory object.
    live_session.expire_all()

    assert live_session.get(EventScore, score_id) is None


@pytestmark_live
def test_live_cii_snapshot_trigger_event_set_null_on_event_delete(live_session):
    session_id = uuid.uuid4()
    event = Event(
        ts=datetime.now(timezone.utc),
        signal_type="network_flow",
        timing_provenance=TIMING_PROVENANCE_CAPTURE_SECONDS,
        replay_session_id=session_id,
        source_row_id="row-snapshot-trigger",
    )
    live_session.add(event)
    live_session.commit()

    snap = CiiSnapshot(
        ts=datetime.now(timezone.utc),
        origin_asset="Power_Substation",
        cii_median=0.5,
        trigger_event_id=event.id,
    )
    live_session.add(snap)
    live_session.commit()
    snap_id = snap.id

    live_session.delete(event)
    live_session.commit()
    # DB-level ON DELETE SET NULL updated `snap` as a side effect of
    # deleting `event` - expire so get() re-queries instead of returning
    # the stale in-memory trigger_event_id.
    live_session.expire_all()

    refreshed = live_session.get(CiiSnapshot, snap_id)
    assert refreshed is not None
    assert refreshed.trigger_event_id is None

    live_session.delete(refreshed)
    live_session.commit()


@pytestmark_live
def test_live_alert_cii_snapshot_set_null_on_snapshot_delete(live_session):
    snap = CiiSnapshot(ts=datetime.now(timezone.utc), origin_asset="Power_Substation", cii_median=0.3)
    live_session.add(snap)
    live_session.commit()

    alert = Alert(
        ts=datetime.now(timezone.utc),
        severity="HIGH",
        asset="Power_Substation",
        title="test alert",
        cii_snapshot_id=snap.id,
        acknowledged=False,
    )
    live_session.add(alert)
    live_session.commit()
    alert_id = alert.id

    live_session.delete(snap)
    live_session.commit()
    live_session.expire_all()

    refreshed = live_session.get(Alert, alert_id)
    assert refreshed is not None
    assert refreshed.cii_snapshot_id is None

    live_session.delete(refreshed)
    live_session.commit()


@pytestmark_live
def test_live_prune_events_keeps_most_recent_n(live_session):
    from backend.retention import prune_events

    session_id = uuid.uuid4()
    base = datetime.now(timezone.utc)
    ids = []
    for i in range(10):
        event = Event(
            ts=base + timedelta(seconds=i),
            signal_type="network_flow",
            timing_provenance=TIMING_PROVENANCE_CAPTURE_SECONDS,
            replay_session_id=session_id,
            source_row_id=f"prune-row-{i}",
        )
        live_session.add(event)
        live_session.flush()
        ids.append(event.id)
    live_session.commit()

    deleted = prune_events(live_session, max_rows=4)
    live_session.commit()
    assert deleted >= 6  # at least the 6 oldest of *this* batch

    remaining_from_batch = (
        live_session.query(Event.source_row_id)
        .filter(Event.replay_session_id == session_id)
        .all()
    )
    remaining_row_ids = {r[0] for r in remaining_from_batch}
    # Only the newest of this batch (row 6..9) can possibly still be present;
    # none of the oldest (0..3) should be.
    assert not ({"prune-row-0", "prune-row-1", "prune-row-2", "prune-row-3"} & remaining_row_ids)

    # cleanup any leftovers from this batch
    live_session.query(Event).filter(Event.replay_session_id == session_id).delete(synchronize_session=False)
    live_session.commit()
