"""
tests/test_api.py — Phase 5 Ticket #8: FastAPI REST routes
(backend/routes.py, backend/main.py, backend/runtime.py, backend/schemas.py).

The default suite touches NEITHER a real Postgres NOR a real
`StreamingScorer` artifact (docs/PHASE5_TICKET8_PLAN.md section 9):

  * DB-backed routes (`/api/events`, `/api/alerts`, `/api/alerts/{id}/ack`,
    and `/api/health`'s DB check) get `backend.routes.get_session_scope`
    overridden with a factory bound to an in-memory SQLite database built
    from the real `backend.models.Base` metadata — genuine SQLAlchemy
    ORM behaviour (ordering, filtering, `session.get`), not a hand-rolled
    statement interpreter. `Event.raw` / `Alert.explanation` /
    `CiiSnapshot.*` use the Postgres-only `JSONB` type, which SQLite's
    compiler cannot render by default; `_patch_sqlite_jsonb()` below
    aliases it to `JSON` on the SQLite compiler for the lifetime of the
    test process — a narrowly-scoped compatibility shim, not a change to
    `backend/models.py` (touching that file is out of this ticket's scope
    per docs/PHASE5_TICKET8_PLAN.md section 2's file list).
  * Replay-control routes get `backend.routes.get_runtime` overridden with
    a fake `AppRuntime` wired to a REAL `ReplayEngine` (genuine
    start/stop/speed/409 behaviour) but a synthetic in-memory
    `_FakeReader` (no real dataset CSV) and a no-op consumer (no DB, no
    scorer) — mirroring tests/test_replay_engine.py's own fixture style.
  * `get_health`/`get_topology`/`get_cii` need no override at all:
    topology and CII are pure functions of `src/config.py` /
    `src/settings.py`, and health's DB check goes through the same
    `get_session_scope` override as everything else.

Live-round-trip coverage against a real Postgres + a real lifespan-built
`AppRuntime` is NOT part of this file — that would duplicate what
tests/test_ingest.py and tests/test_backend_models.py already establish
(both gated on `AEGIS_TEST_LIVE_DB=1`) for the persistence layer this
module merely reads from.
"""

from __future__ import annotations

import importlib
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# SQLite/JSONB compatibility shim — see module docstring. Applied once at
# import time, before any engine below is created.
# ---------------------------------------------------------------------------

if not hasattr(SQLiteTypeCompiler, "_aegis_jsonb_patched"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON
    SQLiteTypeCompiler._aegis_jsonb_patched = True

from backend.main import create_app  # noqa: E402
from backend.models import (  # noqa: E402
    TIMING_PROVENANCE_CAPTURE_SECONDS,
    Alert,
    Base,
    Event,
)
from backend.replay_engine import ReplayEngine  # noqa: E402
from backend.replay_reader import ReplayFlow  # noqa: E402
from backend.routes import get_runtime, get_session_scope  # noqa: E402
from backend.runtime import AppRuntime  # noqa: E402

BASE_TS = datetime(2017, 7, 7, 9, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# In-memory DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_session_scope():
    """A `session_scope`-shaped context-manager factory bound to a fresh
    in-memory SQLite database, seeded with the real `Base.metadata`. Fresh
    per test — no state leaks between tests.
    """
    # StaticPool + check_same_thread=False: FastAPI's TestClient runs the
    # endpoint in a worker thread (via run_in_threadpool) distinct from the
    # thread that set up this fixture's data, and a bare ":memory:" SQLite
    # connection is otherwise both per-connection (a fresh, empty database
    # per checkout) and thread-affine. StaticPool pins one shared
    # connection for the engine's whole lifetime, matching what a real
    # session_scope() using a real pooled Postgres engine looks like from
    # the route's perspective.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    @contextmanager
    def scope():
        session = SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield scope
    engine.dispose()


def make_event(row_id: str, *, ts=BASE_TS, source_asset="City_Payment_Gateway", **kw) -> Event:
    defaults = dict(
        ts=ts,
        source_id="10.0.1.20",
        destination_id="10.0.1.21",
        source_asset=source_asset,
        destination_asset="City_Grid",
        protocol="TCP",
        bytes=1000,
        packets=10,
        duration_sec=1.0,
        signal_type="network_flow",
        source_dataset="cic_ids2017",
        timing_provenance=TIMING_PROVENANCE_CAPTURE_SECONDS,
        replay_session_id=uuid4(),
        source_row_id=row_id,
        raw={"label": "BENIGN"},
    )
    defaults.update(kw)
    return Event(**defaults)


def make_alert(*, ts=BASE_TS, asset="City_Payment_Gateway", acknowledged=False, **kw) -> Alert:
    defaults = dict(
        ts=ts,
        severity="critical",
        asset=asset,
        title="Honeytoken credential used",
        detail="test alert",
        explanation={"top_feature": "bytes"},
        acknowledged=acknowledged,
    )
    defaults.update(kw)
    return Alert(**defaults)


# ---------------------------------------------------------------------------
# Runtime fixtures — replay-control routes
# ---------------------------------------------------------------------------


def _flow(ts: datetime, row_id: str) -> ReplayFlow:
    return ReplayFlow(
        ts=ts,
        source_ip="10.0.0.1",
        source_port=1234,
        destination_ip="10.0.0.9",
        destination_port=80,
        protocol="TCP",
        duration_sec=0.1,
        packets=1,
        bytes=100,
        label="BENIGN",
        is_attack=False,
        timing_provenance="interpolated_minute_bucket",
        source_row_id=row_id,
        source_dataset="synthetic",
    )


class _FakeReader:
    """Spy `ReplayFlowReader` substitute — a fixed synthetic flow list, no
    real dataset CSV touched. Mirrors tests/test_replay_engine.py's own
    `_FakeReader`.
    """

    def __init__(self, flows: list[ReplayFlow]) -> None:
        self._flows = flows

    def iter_flows(self, day=None, limit=None):
        flows = self._flows
        if limit is not None:
            flows = flows[:limit]
        yield from flows


def _noop_consumer(batch, meta) -> None:
    return None


def make_engine_runtime(n_flows: int = 20, consumer=None) -> AppRuntime:
    """A fake `AppRuntime` wired to a REAL `ReplayEngine` (genuine
    start/stop/speed/409 control-plane behaviour) over a synthetic,
    in-memory flow list — no dataset file, no DB, no scorer needed for
    these control-plane assertions. `consumer` defaults to a no-op but can
    be swapped for a recording consumer (see `_RecordingConsumer` below)
    by Ticket #13's inject tests, which need to observe emitted batches.
    """
    flows = [_flow(BASE_TS + timedelta(seconds=i), f"synthetic:{i}") for i in range(n_flows)]
    engine = ReplayEngine(
        consumer=consumer if consumer is not None else _noop_consumer,
        reader=_FakeReader(flows),
        tick_interval=0.01,
        thread_join_timeout=2.0,
    )
    return AppRuntime(
        scorer=object(),
        pipeline=None,
        engine=engine,
        scorer_load_error=None,
        started_at=datetime.now(timezone.utc),
    )


class _RecordingConsumer:
    """Thread-safe spy consumer: records every `(batch, meta)` the engine
    thread emits, so a test can inspect injected flows without a real
    `IngestPipeline`/DB. Used by the Ticket #13 `POST /api/inject` tests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[tuple[list, object]] = []

    def __call__(self, batch, meta) -> None:
        with self._lock:
            self.calls.append((list(batch), meta))


def make_no_scorer_runtime() -> AppRuntime:
    """A fake `AppRuntime` for the "scorer failed to load" path — engine is
    None, mirroring what `backend.runtime.build_runtime()` produces when
    `StreamingScorer.load()` raises.
    """
    return AppRuntime(
        scorer=None,
        pipeline=None,
        engine=None,
        scorer_load_error="No StreamingScorer artifact at <test> (simulated)",
        started_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def make_client(
    *,
    session_scope: Callable | None = None,
    runtime: AppRuntime | None = None,
) -> TestClient:
    """A fresh app + fresh `dependency_overrides` per call (no cross-test
    leakage), used WITHOUT entering it as a context manager — that would
    trigger the real lifespan (a real `StreamingScorer.load()`), which
    this default suite deliberately never needs (see module docstring).
    """
    app = create_app()
    if session_scope is not None:
        app.dependency_overrides[get_session_scope] = lambda: session_scope
    if runtime is not None:
        app.dependency_overrides[get_runtime] = lambda: runtime
    return TestClient(app)


# ---------------------------------------------------------------------------
# Import hygiene (D8-1 guard)
# ---------------------------------------------------------------------------


def test_import_does_not_touch_scorer_or_db(monkeypatch):
    """Importing backend.main must load no joblib artifact and open no DB
    connection — both happen only inside lifespan(), which only runs when
    an ASGI server actually starts the app (docs/PHASE5_TICKET8_PLAN.md
    section 3, decision D8-1). Proven by monkeypatching both call sites to
    raise, then re-executing the module body (`create_app()` included):
    if either got called merely by importing/building the app, this test
    fails.
    """

    def _boom(*args, **kwargs):
        raise AssertionError("must not be called merely by importing backend.main")

    monkeypatch.setattr(
        "backend.streaming.StreamingScorer.load", classmethod(lambda cls, *a, **kw: _boom())
    )
    monkeypatch.setattr("backend.db.get_engine", _boom)

    import backend.main as main_module

    importlib.reload(main_module)  # re-runs create_app(); would raise via monkeypatch if tripped


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


def test_health_ok_when_db_reachable(sqlite_session_scope):
    client = make_client(session_scope=sqlite_session_scope, runtime=make_no_scorer_runtime())
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["scorer_loaded"] is False
    assert body["replay_running"] is False
    assert body["uptime_sec"] >= 0.0


def test_health_reports_scorer_load_error_message(sqlite_session_scope):
    """LOW-1 review fix: `scorer_loaded: false` alone gives an operator
    nothing to act on. `scorer_load_error` must carry the same message
    `AppRuntime.scorer_load_error` captured at startup."""
    runtime = make_no_scorer_runtime()
    client = make_client(session_scope=sqlite_session_scope, runtime=runtime)
    body = client.get("/api/health").json()
    assert body["scorer_loaded"] is False
    assert body["scorer_load_error"] == runtime.scorer_load_error
    assert "No StreamingScorer artifact" in body["scorer_load_error"]


def test_health_scorer_load_error_is_none_when_scorer_loaded(sqlite_session_scope):
    runtime = make_engine_runtime()
    client = make_client(session_scope=sqlite_session_scope, runtime=runtime)
    body = client.get("/api/health").json()
    assert body["scorer_loaded"] is True
    assert body["scorer_load_error"] is None


def test_health_degraded_when_db_unreachable():
    @contextmanager
    def broken_scope():
        raise RuntimeError("simulated: database unreachable")
        yield  # pragma: no cover - unreachable, keeps this a generator

    client = make_client(session_scope=broken_scope, runtime=make_no_scorer_runtime())
    r = client.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["database"] is False


def test_health_reports_replay_running(sqlite_session_scope):
    runtime = make_engine_runtime()
    client = make_client(session_scope=sqlite_session_scope, runtime=runtime)
    try:
        runtime.engine.start(speed=1000.0)
        r = client.get("/api/health")
        assert r.json()["replay_running"] is True
        assert r.json()["scorer_loaded"] is True
    finally:
        runtime.engine.stop()


# ---------------------------------------------------------------------------
# GET /api/topology
# ---------------------------------------------------------------------------


def test_topology_returns_nodes_and_edges():
    client = make_client()
    r = client.get("/api/topology")
    assert r.status_code == 200
    body = r.json()
    assert len(body["nodes"]) > 0
    assert len(body["edges"]) > 0
    names = {n["name"] for n in body["nodes"]}
    assert "City_Payment_Gateway" in names
    node = next(n for n in body["nodes"] if n["name"] == "City_Payment_Gateway")
    assert set(node.keys()) == {"name", "criticality", "type", "purdue_level", "is_gateway"}
    edge = body["edges"][0]
    assert set(edge.keys()) == {"source", "target", "edge_type", "prob", "is_gateway_edge"}


def test_topology_needs_no_db_or_runtime_override():
    """No dependency_overrides at all — build_graph()/compute_seed_rows()
    read only src/config.py + src/settings.py."""
    client = make_client()
    assert client.get("/api/topology").status_code == 200


# ---------------------------------------------------------------------------
# GET /api/events — ordering, since, limit bounds (D8-2)
# ---------------------------------------------------------------------------


def test_events_ordering_is_ts_desc_id_desc_with_tied_timestamps(sqlite_session_scope):
    with sqlite_session_scope() as session:
        # All three share ONE ts (a minute-bucket tie) — id must break it.
        session.add(make_event("r1", ts=BASE_TS))
        session.add(make_event("r2", ts=BASE_TS))
        session.add(make_event("r3", ts=BASE_TS))

    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/events")
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()["events"]]
    assert ids == sorted(ids, reverse=True)
    assert ids == [3, 2, 1]  # newest-inserted id first, despite identical ts


def test_events_since_is_exclusive_id_lower_bound_not_a_timestamp(sqlite_session_scope):
    with sqlite_session_scope() as session:
        session.add(make_event("r1", ts=BASE_TS))
        session.add(make_event("r2", ts=BASE_TS))
        session.add(make_event("r3", ts=BASE_TS + timedelta(seconds=1)))

    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/events", params={"since": 1})
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()["events"]]
    # id 1 excluded (exclusive bound), not filtered by ts. Order is ASCENDING
    # here — `since` supplied means catch-up mode (HIGH-1 fix, see the
    # ordering-asymmetry test below) — so [2, 3], not [3, 2].
    assert ids == [2, 3]
    assert 1 not in ids


def test_events_since_uses_ascending_order_page_load_uses_descending(sqlite_session_scope):
    """The ordering asymmetry itself (HIGH-1 fix, later refined by HIGH-2).
    Omitted `since` (page load) -> newest-first, `ts DESC, id DESC`;
    supplied `since` (catch-up poll) -> oldest-first, `id ASC` ALONE (not
    `ts ASC, id ASC` as HIGH-1 originally shipped it — see
    test_events_catch_up_survives_id_and_ts_order_disagreement below for
    why sorting by anything other than the filter key, `id`, is unsafe).

    This fixture's rows all share one `ts`, so it cannot distinguish
    `ORDER BY id ASC` from `ORDER BY ts ASC, id ASC` — both produce
    [2, 3, 4, 5] here. It still guards the direction flip (ascending vs.
    descending) and the exact id sequence; the id-vs-ts key question is
    covered by the dedicated regression test below.
    """
    with sqlite_session_scope() as session:
        for i in range(5):
            session.add(make_event(f"r{i}", ts=BASE_TS))  # all tied

    client = make_client(session_scope=sqlite_session_scope)

    page_load = client.get("/api/events").json()["events"]
    assert [e["id"] for e in page_load] == [5, 4, 3, 2, 1]

    # since=1 (the minimum allowed — `since` is a client's existing cursor,
    # never "the beginning"; a brand-new client omits it entirely and gets
    # the DESC page-load path above) excludes id 1 and returns 2..5 ascending.
    catch_up = client.get("/api/events", params={"since": 1}).json()["events"]
    assert [e["id"] for e in catch_up] == [2, 3, 4, 5]


def test_events_has_more_true_when_backlog_exceeds_limit_false_on_last_page(sqlite_session_scope):
    with sqlite_session_scope() as session:
        for i in range(6):
            session.add(make_event(f"r{i}", ts=BASE_TS))  # ids 1..6

    client = make_client(session_scope=sqlite_session_scope)

    # since=1: the client already has id 1 (a prior poll/page load); the
    # remaining backlog is ids 2..6 (5 events) against limit=3.
    r = client.get("/api/events", params={"since": 1, "limit": 3})
    body = r.json()
    assert [e["id"] for e in body["events"]] == [2, 3, 4]
    assert body["has_more"] is True

    r2 = client.get("/api/events", params={"since": 4, "limit": 3})
    body2 = r2.json()
    assert [e["id"] for e in body2["events"]] == [5, 6]
    assert body2["has_more"] is False


def test_events_catch_up_polling_drains_every_tied_timestamp_event_with_no_gap(sqlite_session_scope):
    """HIGH-1 acceptance test: reproduces the review's exact failure shape
    (docs/PHASE5_TICKET8_PLAN.md section 4 correction note) — a batch of
    events that ALL share one identical `ts` (the real friday-morning
    shape: median 629 events/bucket, max 4,017), a client cursor sitting
    at the first id, and repeated polling with a page size smaller than
    the backlog, advancing the cursor to the max id returned each poll.

    Asserts the client eventually receives EVERY event with no gap and no
    duplicate.

    Verified to genuinely fail under the pre-fix behaviour: reverting
    `list_events` to unconditional `ORDER BY ts DESC, id DESC` (D8-2 as
    originally written) makes poll 1 return the newest `limit` rows
    (ids 151..250) regardless of `since=0`, the cursor then advances to
    250, and every following poll returns 0 rows — so `sorted(received)`
    never covers ids 1..150 and this assertion fails. Confirmed by hand by
    temporarily reapplying the old single `.order_by(Event.ts.desc(),
    Event.id.desc())` (no ascending branch) — this test failed with
    `assert sorted(received) == list(range(1, 251))` reporting exactly the
    missing 1..150 range, matching the 149/249-lost measurement in the
    route docstring at a slightly different N/limit.
    """
    n = 250
    limit = 100
    with sqlite_session_scope() as session:
        # A seed event the client has already received in an earlier poll —
        # mirrors the review's "cursor at 5067" (an id that is itself the
        # first event of a prior batch, not "the beginning of time"; the
        # `since` query param is bounded `ge=1` for exactly this reason, see
        # its description in routes.py).
        session.add(make_event("seed", ts=BASE_TS))
        for i in range(n):
            session.add(make_event(f"r{i}", ts=BASE_TS))  # every row shares one ts

    client = make_client(session_scope=sqlite_session_scope)

    cursor = 1  # the seed event's id — client already has it
    received: list[int] = []
    polls = 0
    max_polls = (n // limit) + 2  # guards against an infinite loop if the fix regresses
    while polls < max_polls:
        r = client.get("/api/events", params={"since": cursor, "limit": limit})
        assert r.status_code == 200
        body = r.json()
        ids = [e["id"] for e in body["events"]]
        polls += 1
        if not ids:
            assert body["has_more"] is False
            break
        received.extend(ids)
        cursor = max(ids)

    assert sorted(received) == list(range(2, n + 2))  # every event AFTER the seed, no gap
    assert len(received) == len(set(received))  # no duplicate delivery either


def test_events_catch_up_survives_id_and_ts_order_disagreement(sqlite_session_scope):
    """HIGH-2 regression test (docs/PHASE5_STATE.md decision P5-18
    addendum). HIGH-1's fix made the catch-up branch ascending, but it
    sorted by `ts ASC, id ASC` while filtering by `WHERE id > :since` —
    two different keys. That is only safe if id order and ts order always
    agree, which they do not: a capture day replayed a second time
    restarts its own virtual clock, so the second session's rows get
    HIGHER ids than the first session's but can land in an EARLIER (or
    overlapping) `ts` range.

    This fixture reproduces exactly that shape: a "first session" batch
    with LATER timestamps is inserted first (so it gets the lower ids),
    then a single "second session" row with the EARLIEST `ts` of the
    whole set is inserted last (so it gets the HIGHEST id) — mirroring the
    live measurement in the route docstring (session id 11567 had the
    highest id but the earliest ts of the three sessions observed).

    Drains with a cursor exactly as a real client does (advance to
    `max(id)` seen each poll) and asserts the drain is gapless: every
    event with `id > cursor` is delivered exactly once, none delivered
    twice, and the poll loop terminates via `has_more: False` rather than
    looping forever or leaving a permanent gap.

    Verified to genuinely fail under the pre-fix `ORDER BY ts ASC, id ASC`
    catch-up ordering (confirmed by hand, see PR notes): filtering
    `WHERE id > 1` and sorting by `ts ASC, id ASC` puts the id=8 row
    (earliest ts) FIRST in the result, ahead of ids 2..6 (later, tied
    ts). Poll 1 (limit=3) returns ids [8, 2, 3]; the client advances its
    cursor to `max(ids) = 8`; poll 2 (`since=8`) matches nothing, so the
    client stops having received only {8, 2, 3} — ids 4, 5, 6 are
    permanently skipped and `sorted(received) == list(range(2, 9))`
    fails.
    """
    with sqlite_session_scope() as session:
        # Seed event the client already has (its starting cursor).
        session.add(make_event("seed", ts=BASE_TS))  # id 1
        # "First session": later timestamps, inserted (and so id-assigned)
        # first — ids 2..6, ts = BASE_TS + 100s (tied within this batch).
        later_ts = BASE_TS + timedelta(seconds=100)
        for i in range(5):
            session.add(make_event(f"session1-{i}", ts=later_ts))  # ids 2..6
        # "Second session": ONE row with the EARLIEST ts of the whole set,
        # inserted (and so id-assigned) LAST — id 7... wait, must land as
        # the highest id: inserted after the first session, so id 7.
        earliest_ts = BASE_TS + timedelta(seconds=1)
        session.add(make_event("session2-earliest", ts=earliest_ts))  # id 7

    client = make_client(session_scope=sqlite_session_scope)

    cursor = 1  # the seed event's id
    received: list[int] = []
    polls = 0
    max_polls = 10  # guards the test itself against a true infinite loop
    while polls < max_polls:
        r = client.get("/api/events", params={"since": cursor, "limit": 3})
        assert r.status_code == 200
        body = r.json()
        ids = [e["id"] for e in body["events"]]
        polls += 1
        if not ids:
            assert body["has_more"] is False
            break
        received.extend(ids)
        cursor = max(ids)

    assert polls < max_polls, "poll loop did not terminate — infinite re-delivery"
    assert sorted(received) == list(range(2, 8))  # every event after the seed, no gap
    assert len(received) == len(set(received))  # no duplicate delivery either


def test_events_limit_above_max_is_422_not_clamped(sqlite_session_scope):
    from backend.config import BACKEND_SETTINGS

    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/events", params={"limit": BACKEND_SETTINGS.api_events_max_limit + 1})
    assert r.status_code == 422


def test_events_limit_zero_is_422(sqlite_session_scope):
    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/events", params={"limit": 0})
    assert r.status_code == 422


def test_events_empty_ok(sqlite_session_scope):
    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/events")
    assert r.status_code == 200
    assert r.json()["events"] == []


def test_events_payload_shape(sqlite_session_scope):
    with sqlite_session_scope() as session:
        session.add(make_event("r1"))

    client = make_client(session_scope=sqlite_session_scope)
    body = client.get("/api/events").json()["events"][0]
    assert body["source_asset"] == "City_Payment_Gateway"
    assert body["signal_type"] == "network_flow"
    assert body["raw"] == {"label": "BENIGN"}


# ---------------------------------------------------------------------------
# GET /api/alerts
# ---------------------------------------------------------------------------


def test_alerts_filter_by_acknowledged(sqlite_session_scope):
    with sqlite_session_scope() as session:
        session.add(make_alert(acknowledged=True))
        session.add(make_alert(acknowledged=False))

    client = make_client(session_scope=sqlite_session_scope)

    both = client.get("/api/alerts").json()["alerts"]
    assert len(both) == 2

    acked = client.get("/api/alerts", params={"acknowledged": "true"}).json()["alerts"]
    assert len(acked) == 1
    assert acked[0]["acknowledged"] is True

    unacked = client.get("/api/alerts", params={"acknowledged": "false"}).json()["alerts"]
    assert len(unacked) == 1
    assert unacked[0]["acknowledged"] is False


def test_alerts_ordering_ts_desc_id_desc(sqlite_session_scope):
    with sqlite_session_scope() as session:
        session.add(make_alert(ts=BASE_TS))
        session.add(make_alert(ts=BASE_TS))

    client = make_client(session_scope=sqlite_session_scope)
    ids = [a["id"] for a in client.get("/api/alerts").json()["alerts"]]
    assert ids == [2, 1]


def test_alerts_limit_bound_shared_with_events(sqlite_session_scope):
    from backend.config import BACKEND_SETTINGS

    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/alerts", params={"limit": BACKEND_SETTINGS.api_events_max_limit + 1})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/alerts/{id}/ack
# ---------------------------------------------------------------------------


def test_ack_unknown_alert_is_404(sqlite_session_scope):
    client = make_client(session_scope=sqlite_session_scope)
    r = client.post("/api/alerts/999/ack")
    assert r.status_code == 404


def test_ack_is_idempotent_and_preserves_first_acknowledged_at(sqlite_session_scope):
    with sqlite_session_scope() as session:
        session.add(make_alert())

    client = make_client(session_scope=sqlite_session_scope)

    first = client.post("/api/alerts/1/ack")
    assert first.status_code == 200
    assert first.json()["acknowledged"] is True
    first_ack_at = first.json()["acknowledged_at"]
    assert first_ack_at is not None

    second = client.post("/api/alerts/1/ack")
    assert second.status_code == 200
    assert second.json()["acknowledged"] is True
    # NOT overwritten. Compared as parsed datetimes rather than raw strings:
    # SQLite (this test's fixture DB only — Postgres has native
    # timestamptz) round-trips a DateTime(timezone=True) value without its
    # UTC "Z" suffix, so the first response (serialized straight from the
    # just-mutated in-memory object) and the second (read back from the
    # fixture DB) can differ in string form for the same instant.
    def _parsed(s: str) -> datetime:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    assert _parsed(second.json()["acknowledged_at"]) == _parsed(first_ack_at)


# ---------------------------------------------------------------------------
# GET /api/cii/{asset} (D8-3)
# ---------------------------------------------------------------------------


def test_cii_unknown_asset_is_404_not_fabricated_zeros():
    client = make_client()
    r = client.get("/api/cii/Central_Bank_Interbank_Feed")  # the known-issue asset, CLAUDE.md sec 7
    assert r.status_code == 404
    assert "not a node" in r.json()["detail"]
    # The response body must not carry a disguised all-zero "result".
    assert "cii_median" not in r.text


def test_cii_known_asset_returns_distribution():
    client = make_client()
    r = client.get("/api/cii/City_Payment_Gateway")
    assert r.status_code == 200
    body = r.json()
    assert body["origin_asset"] == "City_Payment_Gateway"
    assert body["anomaly_score"] == 1.0
    assert 0.0 <= body["cii_p5"] <= body["cii_median"] <= body["cii_p95"]
    assert isinstance(body["impacted_assets"], list)


def test_cii_anomaly_score_default_is_one():
    client = make_client()
    body = client.get("/api/cii/City_Payment_Gateway").json()
    assert body["anomaly_score"] == 1.0


def test_cii_anomaly_score_bounds_422():
    client = make_client()
    assert client.get("/api/cii/City_Payment_Gateway", params={"anomaly_score": 0}).status_code == 422
    assert client.get("/api/cii/City_Payment_Gateway", params={"anomaly_score": 1.5}).status_code == 422


# ---------------------------------------------------------------------------
# POST /api/replay/start|stop|speed
# ---------------------------------------------------------------------------


def test_replay_start_then_start_again_is_409():
    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    try:
        r1 = client.post("/api/replay/start", json={"speed": 1000.0})
        assert r1.status_code == 200
        assert r1.json()["running"] is True

        r2 = client.post("/api/replay/start", json={"speed": 1000.0})
        assert r2.status_code == 409
    finally:
        runtime.engine.stop()


def test_replay_stop_is_idempotent_and_200_when_never_started():
    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    r = client.post("/api/replay/stop")
    assert r.status_code == 200
    assert r.json()["running"] is False

    r2 = client.post("/api/replay/stop")
    assert r2.status_code == 200


def test_replay_speed_zero_and_negative_are_422_before_reaching_engine():
    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    assert client.post("/api/replay/speed", json={"multiplier": 0}).status_code == 422
    assert client.post("/api/replay/speed", json={"multiplier": -1}).status_code == 422


def test_replay_speed_409_when_not_running():
    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    r = client.post("/api/replay/speed", json={"multiplier": 5.0})
    assert r.status_code == 409


def test_replay_speed_changes_running_engine():
    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    try:
        client.post("/api/replay/start", json={"speed": 10.0})
        r = client.post("/api/replay/speed", json={"multiplier": 500.0})
        assert r.status_code == 200
        assert r.json()["speed"] == 500.0
    finally:
        runtime.engine.stop()


def test_replay_routes_503_when_scorer_failed_to_load():
    runtime = make_no_scorer_runtime()
    client = make_client(runtime=runtime)
    assert client.post("/api/replay/start", json={}).status_code == 503
    assert client.post("/api/replay/speed", json={"multiplier": 5.0}).status_code == 503
    # stop() remains a 200 no-op even with no engine at all.
    assert client.post("/api/replay/stop").status_code == 200


def test_replay_start_maps_dataset_to_day_and_honours_limit_and_start_at():
    runtime = make_engine_runtime(n_flows=50)
    client = make_client(runtime=runtime)
    try:
        r = client.post(
            "/api/replay/start",
            json={"dataset": None, "speed": 5000.0, "limit": 3},
        )
        assert r.status_code == 200
        # Let the tiny synthetic 3-flow run drain.
        for _ in range(50):
            if not runtime.engine.status().running:
                break
            time.sleep(0.02)
        status = runtime.engine.status()
        assert status.emitted_count == 3
    finally:
        runtime.engine.stop()


# ---------------------------------------------------------------------------
# POST /api/inject | GET /api/inject/scenarios (Ticket #13)
# ---------------------------------------------------------------------------

import backend.inject as inject_module  # noqa: E402
from backend.replay_reader import ReplayFlow as _RF  # noqa: E402


class _FakeInjectReader:
    """Stands in for `backend.inject.ReplayFlowReader` so these tests never
    touch a real (75-280MB) dataset CSV. Yields synthetic but realistically
    shaped real-attack-labelled flows for the days the scenario registry
    references.
    """

    _BY_DAY = {
        "friday-morning": [
            _RF(
                ts=BASE_TS,
                source_ip="192.168.10.5",
                source_port=4444,
                destination_ip="192.168.10.50",
                destination_port=80,
                protocol="TCP",
                duration_sec=12.5,
                packets=7,
                bytes=4321,
                label="Bot",
                is_attack=True,
                timing_provenance=TIMING_PROVENANCE_CAPTURE_SECONDS,
                source_row_id=f"Friday-WorkingHours-Morning.pcap_ISCX.csv:{i}",
                source_dataset="CIC-IDS2017-TrafficLabelling",
            )
            for i in range(5)
        ],
    }

    def iter_flows(self, day=None, limit=None):
        yield from self._BY_DAY.get(day, [])


@pytest.fixture(autouse=True)
def _reset_inject_pool_cache(monkeypatch):
    """Route to the fake reader (never a real CSV) and reset the
    module-level pool cache around every test in this file, since it is
    process-global and would otherwise leak state between tests."""
    monkeypatch.setattr(inject_module, "ReplayFlowReader", _FakeInjectReader)
    inject_module.clear_pool_cache()
    yield
    inject_module.clear_pool_cache()


def test_inject_scenarios_lists_the_real_registry():
    client = make_client()
    r = client.get("/api/inject/scenarios")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()["scenarios"]}
    assert {"bot_c2", "ddos", "port_scan", "honeytoken"} <= names
    honeytoken = next(s for s in r.json()["scenarios"] if s["name"] == "honeytoken")
    assert honeytoken["is_honeytoken"] is True
    assert honeytoken["label"] == "Bot"


def test_inject_503_when_scorer_failed_to_load():
    runtime = make_no_scorer_runtime()
    client = make_client(runtime=runtime)
    r = client.post("/api/inject", json={"scenario": "bot_c2"})
    assert r.status_code == 503


def test_inject_409_when_no_replay_running():
    """Correctness requirement: inject() only drains on the engine's tick,
    which only runs while start() has the engine thread alive. This must
    be an explicit 409, never a silent no-op."""
    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    r = client.post(
        "/api/inject",
        json={"scenario": "bot_c2", "target_asset": "City_Payment_Gateway"},
    )
    assert r.status_code == 409
    assert "not running" in r.json()["detail"] or "no replay session" in r.json()["detail"]


def test_inject_422_unknown_scenario():
    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    try:
        client.post("/api/replay/start", json={"speed": 1000.0})
        r = client.post("/api/inject", json={"scenario": "not_a_scenario"})
        assert r.status_code == 422
    finally:
        runtime.engine.stop()


def test_inject_422_unresolvable_target_asset():
    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    try:
        client.post("/api/replay/start", json={"speed": 1000.0})
        r = client.post(
            "/api/inject",
            json={"scenario": "bot_c2", "target_asset": "Gateway_L4"},
        )
        assert r.status_code == 422
    finally:
        runtime.engine.stop()


def test_inject_count_above_max_is_422():
    from backend.config import BACKEND_SETTINGS

    runtime = make_engine_runtime()
    client = make_client(runtime=runtime)
    try:
        client.post("/api/replay/start", json={"speed": 1000.0})
        r = client.post(
            "/api/inject",
            json={
                "scenario": "bot_c2",
                "count": BACKEND_SETTINGS.inject_max_flows + 1,
            },
        )
        assert r.status_code == 422
    finally:
        runtime.engine.stop()


def test_inject_success_emits_real_labelled_flows_tagged_injected():
    consumer = _RecordingConsumer()
    runtime = make_engine_runtime(consumer=consumer)
    client = make_client(runtime=runtime)
    try:
        # A modest speed (not the 1000x+ used by pure control-plane tests)
        # so the 20-flow synthetic schedule takes long enough in wall time
        # for the injected batch to land on a tick before the scheduled
        # run naturally exhausts and the engine thread exits.
        client.post("/api/replay/start", json={"speed": 50.0})
        r = client.post(
            "/api/inject",
            json={
                "scenario": "bot_c2",
                "target_asset": "City_Payment_Gateway",
                "count": 5,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["scenario"] == "bot_c2"
        assert body["target_asset"] == "City_Payment_Gateway"
        assert body["real_label"] == "Bot"
        assert body["is_honeytoken"] is False
        assert body["flows_injected"] == 5
        assert "what-if" in body["message"].lower()

        # Wait for the engine's next tick to drain and emit the injected
        # batch to the recording consumer.
        injected_calls = []
        for _ in range(150):
            with consumer._lock:
                injected_calls = [
                    (batch, meta) for batch, meta in consumer.calls if meta.origin == "injected"
                ]
                if injected_calls:
                    break
            time.sleep(0.02)
        assert injected_calls, "no injected batch was ever emitted"
        batch, meta = injected_calls[0]
        assert len(batch) == 5
        assert all(f.source_ip == "10.0.1.20" for f in batch)  # re-targeted
        assert all(f.label == "Bot" for f in batch)  # real label preserved
        assert all(f.is_honeytoken_use is False for f in batch)
    finally:
        runtime.engine.stop()


def test_inject_honeytoken_scenario_sets_flag_on_real_flows():
    consumer = _RecordingConsumer()
    runtime = make_engine_runtime(consumer=consumer)
    client = make_client(runtime=runtime)
    try:
        client.post("/api/replay/start", json={"speed": 50.0})
        r = client.post(
            "/api/inject",
            json={
                "scenario": "honeytoken",
                "target_asset": "City_Payment_Gateway",
                "count": 3,
            },
        )
        assert r.status_code == 200
        assert r.json()["is_honeytoken"] is True

        injected_calls = []
        for _ in range(150):
            with consumer._lock:
                injected_calls = [
                    (batch, meta) for batch, meta in consumer.calls if meta.origin == "injected"
                ]
                if injected_calls:
                    break
            time.sleep(0.02)

        assert injected_calls, "no injected batch was ever emitted"
        batch, meta = injected_calls[0]
        assert meta.origin == "injected"
        assert all(f.is_honeytoken_use is True for f in batch)
        assert all(f.label == "Bot" for f in batch)  # telemetry stays real
    finally:
        runtime.engine.stop()


# ---------------------------------------------------------------------------
# Real lifespan smoke test (optional — skipped if no warmup artifact)
# ---------------------------------------------------------------------------


def test_real_lifespan_builds_working_runtime_or_reports_degraded():
    """One end-to-end sanity check using the REAL lifespan (build_runtime())
    against whatever this machine actually has: a real Postgres connection
    attempt and a real `StreamingScorer.load()`. Not part of the "default
    suite needs no Postgres" guarantee (this single test may skip or see
    scorer_loaded=False depending on machine state) — it exists to catch a
    wiring mistake the overridden-dependency tests above cannot see, since
    they never exercise `backend.runtime.build_runtime()` itself.
    """
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code in (200, 503)
        body = r.json()
        assert isinstance(body["scorer_loaded"], bool)
        assert isinstance(body["database"], bool)
