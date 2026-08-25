"""
backend/routes.py — Phase 5 Ticket #8: the nine REST routes.

    GET  /api/health
    GET  /api/topology
    GET  /api/events?limit=&since=
    GET  /api/alerts?acknowledged=
    POST /api/alerts/{id}/ack
    GET  /api/cii/{asset}
    POST /api/replay/start
    POST /api/replay/stop
    POST /api/replay/speed

Explicitly OUT of scope (docs/PHASE5_TICKET8_PLAN.md section 1): `POST
/api/inject` (Ticket #13), `WS /ws/stream` (Ticket #9), `GET /api/stats`
(Ticket #16). Do not add them here.

Two overridable dependencies carry every route's external state, so the
default test suite needs neither Postgres nor a real `AppRuntime`
(docs/PHASE5_TICKET8_PLAN.md section 9):

  `get_runtime`       -> the process-wide `AppRuntime` (scorer/pipeline/
                          engine), read from `request.app.state.runtime`.
  `get_session_scope` -> the session-scope CONTEXT-MANAGER FACTORY (a
                          zero-arg callable returning a context manager),
                          not a request-scoped session itself. Every
                          DB-touching route calls `with scope() as
                          session:` in its own body. This is deliberately
                          NOT a generator-yielding FastAPI dependency
                          (`Depends(get_db_session)` handing back a live
                          `Session`): an exception raised while such a
                          generator is *opening* a connection propagates
                          as an unhandled 500 before the route body ever
                          runs, which would make `/api/health`'s "DB down
                          -> 503 degraded, not a crash" requirement
                          impossible to implement from inside the route.
                          Routing the DB check through the same
                          `get_session_scope` override used by every other
                          DB route also means health needs no bespoke
                          test-only plumbing of its own.

Invariant D — one graph authority. `/api/topology`, `/api/cii/{asset}`,
and the CII debounce cache inside `IngestPipeline` all ultimately trace
back to `graph_manager.build_graph()` / `backend.seed.compute_seed_rows()`.
This module never builds a second topology or a second criticality map —
`build_criticality_map()` (Ticket #7) is imported from `backend.ingest`,
not re-derived, per the ticket brief's explicit instruction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, ContextManager

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.config import BACKEND_SETTINGS
from backend.db import session_scope
from backend.ingest import build_criticality_map
from backend.models import Alert, Event
from backend.replay_engine import ReplayEngineError, ReplayStatus
from backend.runtime import AppRuntime
from backend.schemas import (
    AlertOut,
    AlertsResponse,
    CiiResponse,
    EventOut,
    EventsResponse,
    HealthResponse,
    ReplaySpeedRequest,
    ReplayStartRequest,
    ReplayStatusResponse,
    TopologyEdge,
    TopologyNode,
    TopologyResponse,
)
from backend.seed import compute_seed_rows
from cii_calculator import compute_cascading_impact_full
from graph_manager import build_graph

router = APIRouter()

#: A `ReplayStatus` snapshot for "no engine has ever existed in this
#: process" (the scorer failed to load — see `backend.runtime.AppRuntime`).
#: Field values mirror exactly what a freshly-constructed, never-started
#: `ReplayEngine` reports from its own `status()`, so `POST /api/replay/stop`
#: stays idempotent (200, not 503) even when there is no engine at all —
#: stopping something that never started is a legitimate no-op.
_NO_ENGINE_STATUS = ReplayStatus(
    running=False,
    day=None,
    speed=None,
    replay_session_id=None,
    emitted_count=0,
    total_for_day=0,
    current_virtual_position=None,
    lag_seconds=0.0,
    batches_emitted=0,
    consumer_error_count=0,
    consumer_failed_flow_count=0,
)


# ---------------------------------------------------------------------------
# Dependencies (overridden in tests/test_api.py)
# ---------------------------------------------------------------------------


def get_runtime(request: Request) -> AppRuntime:
    """The process-wide `AppRuntime`, built once by the lifespan (see
    `backend/main.py`) and stored on `app.state.runtime`. Tests override
    this callable directly with a fake `AppRuntime`, so no route ever needs
    the real lifespan (a real scorer load, a real `ReplayEngine`) to run.
    """
    return request.app.state.runtime


def get_session_scope() -> Callable[[], ContextManager[Session]]:
    """Returns the session-scope context-manager FACTORY — `session_scope`
    itself, uncalled — not a session. See the module docstring for why
    routes call `with scope() as session:` rather than taking a session via
    `Depends`. Tests override this to point at a throwaway database.
    """
    return session_scope


def _require_replay_engine(runtime: AppRuntime):
    """Every replay-control route needs a live `ReplayEngine`. Raises 503
    (not 500 or a silent no-op) when the scorer failed to load at startup —
    see decision D8-1 and `AppRuntime.scorer_load_error`.
    """
    if runtime.engine is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "StreamingScorer failed to load at startup "
                f"({runtime.scorer_load_error}); replay control is "
                "unavailable until a valid artifact exists. Build one with "
                "'PYTHONPATH=src venv/bin/python -m backend.warmup', then "
                "restart. See GET /api/health."
            ),
        )
    return runtime.engine


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


@router.get("/api/health", response_model=HealthResponse)
def get_health(
    response: Response,
    runtime: AppRuntime = Depends(get_runtime),
    scope: Callable[[], ContextManager[Session]] = Depends(get_session_scope),
) -> HealthResponse:
    """Liveness + per-component honesty (docs/PHASE5_TICKET8_PLAN.md
    section 8). `status` is driven solely by a cheap `SELECT 1` against
    Postgres, wrapped so an unreachable database yields `"degraded"` (HTTP
    503) rather than a stack trace; `scorer_loaded` and `replay_running`
    are reported alongside it but do not themselves flip `status` — a
    working, DB-backed deployment with no model artifact yet built is
    still "ok" for every read-only route.

    `scorer_load_error` (LOW-1, review fix) surfaces
    `runtime.scorer_load_error` verbatim — the same message
    `_require_replay_engine` already quotes in its 503 detail — so an
    operator sees *why* `scorer_loaded` is `False` without first having to
    hit a replay route and get a 503 just to read the reason. `None`
    whenever the scorer loaded.
    """
    database_ok = True
    try:
        with scope() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False

    replay_running = runtime.engine.status().running if runtime.engine is not None else False
    uptime_sec = (datetime.now(timezone.utc) - runtime.started_at).total_seconds()

    payload = HealthResponse(
        status="ok" if database_ok else "degraded",
        database=database_ok,
        scorer_loaded=runtime.scorer is not None,
        scorer_load_error=runtime.scorer_load_error,
        replay_running=replay_running,
        uptime_sec=uptime_sec,
    )
    if not database_ok:
        response.status_code = 503
    return payload


# ---------------------------------------------------------------------------
# GET /api/topology
# ---------------------------------------------------------------------------


@router.get("/api/topology", response_model=TopologyResponse)
def get_topology() -> TopologyResponse:
    """Nodes + edges from `graph_manager.build_graph()`, the single graph
    authority (Invariant D). Node metadata (criticality/type/purdue_level/
    is_gateway) comes from `backend.seed.compute_seed_rows()`, not a second
    hand-rolled lookup — that function iterates the SAME `build_graph()`
    call internally, so its node set is guaranteed to match this route's
    graph by construction. No DB required: this works before any seeding
    has happened.
    """
    graph = build_graph(directed=True)
    meta_by_name = {row["name"]: row for row in compute_seed_rows()}

    nodes = [
        TopologyNode(
            name=name,
            criticality=float(meta_by_name[name]["criticality"]),
            type=meta_by_name[name]["type"],
            purdue_level=meta_by_name[name]["purdue_level"],
            is_gateway=bool(meta_by_name[name]["is_gateway"]),
        )
        for name in graph.nodes()
    ]
    edges = [
        TopologyEdge(
            source=u,
            target=v,
            edge_type=data["edge_type"],
            prob=float(data["prob"]),
            is_gateway_edge=bool(data["is_gateway_edge"]),
        )
        for u, v, data in graph.edges(data=True)
    ]
    return TopologyResponse(nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# GET /api/events
# ---------------------------------------------------------------------------


@router.get("/api/events", response_model=EventsResponse)
def list_events(
    limit: int = Query(
        default=BACKEND_SETTINGS.api_events_default_limit,
        ge=1,
        le=BACKEND_SETTINGS.api_events_max_limit,
        description="Max rows to return. Above api_events_max_limit -> 422 (never silently clamped).",
    ),
    since: int | None = Query(
        default=None,
        ge=1,
        description=(
            "Exclusive event-ID lower bound (WHERE id > since) — NOT a "
            "timestamp. Hundreds of events can share one `ts` on "
            "minute-granularity replay days (friday-morning: median 629 "
            "events/bucket, max 4,017), so a timestamp cursor inside a "
            "tied bucket either re-delivers the whole bucket or skips part "
            "of it; the serial `id` is exact because IngestPipeline "
            "inserts each batch in arrival order.\n\n"
            "ORDERING ASYMMETRY — read this before polling. Omitted (page "
            "load): newest-first, `ts DESC, id DESC` — correct for 'show "
            "me the latest N'. Supplied (catch-up poll): OLDEST-FIRST, "
            "`id ASC` ALONE — the filter is `WHERE id > :since`, so the "
            "sort key MUST be `id` too, or the catch-up drain is not "
            "gapless. This is not just an ordering preference: a replay "
            "day can be re-run, and a second replay session's rows get "
            "HIGHER ids than the first session's but can land in the SAME "
            "or an EARLIER `ts` range (each session restarts its own "
            "virtual clock). So id order and ts order can flatly disagree "
            "— sorting by `ts ASC, id ASC` while filtering on `id` "
            "resurfaces exactly the HIGH-1 bug this parameter's docstring "
            "used to describe as fixed, in a new shape (HIGH-2). Measured "
            "on real Postgres, three replay sessions on the same capture "
            "day: session A ids 5062..5066 (ts 14:29..14:30, n=5), session "
            "B ids 5567..11566 (ts 14:29..14:41, n=6000), session C id "
            "11567 alone (ts 14:29 — the EARLIEST ts of the three, but the "
            "HIGHEST id). `since=10567&limit=5` under `ts ASC, id ASC` "
            "returned id 11567 first (earliest ts, sorted to the front) "
            "followed by ids 10568..10571 — a client advancing its cursor "
            "to `max(id)=11567` then skips everything between 10568 and "
            "11566 forever; a client advancing to the last row in *sort* "
            "order (10571) instead re-receives id 11567 on every following "
            "poll forever, since `11567 > 10571` always holds. Draining "
            "from a cursor below every event, this delivered 200 of 6006 "
            "matching events (96.7% lost) before terminating in the "
            "duplicate-id loop. Fixed by making the catch-up sort key "
            "identical to the catch-up filter key: `ORDER BY id ASC` "
            "alone, nothing else, so the drain is gapless by construction "
            "and matches the true ingest/emission order (docs/"
            "PHASE5_STATE.md's 'Note for Ticket #8': the serial `id` "
            "preserves true emission order within a batch; event-time "
            "ordering is a page-load concern, not a catch-up one). See "
            "`has_more` on the response to know when to stop polling, and "
            "docs/PHASE5_STATE.md decision P5-18 (HIGH-2 addendum)."
        ),
    ),
    scope: Callable[[], ContextManager[Session]] = Depends(get_session_scope),
) -> EventsResponse:
    """Recent events, paged. Ordering depends on whether `since` is
    supplied — see that parameter's description for the full rationale.

    Omitted `since` (page load): `ORDER BY ts DESC, id DESC` — the
    newest-first "show me the latest N" view, ties broken by `id`.

    Supplied `since` (catch-up poll): `ORDER BY id ASC` ALONE. This is
    deliberately not `ts ASC, id ASC` (the original HIGH-1 fix, docs/
    PHASE5_TICKET8_PLAN.md section 4's first correction note) — that
    version sorted by a different key than it filtered by (`WHERE id >
    :since` but `ORDER BY ts ASC, id ASC`), and a replayed capture day can
    produce a later session with higher ids but an EARLIER or overlapping
    `ts` range, so the two orderings disagree about which rows come
    "next". Whenever the filter key and the sort key differ, a bounded
    LIMIT can silently skip rows the filter matched but the sort placed
    outside the page, or re-deliver a row forever because it never sorts
    below the client's advancing cursor (HIGH-2, docs/PHASE5_STATE.md
    decision P5-18's addendum — 96.7% event loss measured before this
    fix). Making the sort key `id` — identical to the filter key — makes
    the catch-up drain gapless by construction, and matches true ingest
    order per docs/PHASE5_STATE.md's "Note for Ticket #8": the serial `id`
    is the correct catch-up ordering because it is what
    `IngestPipeline` actually inserted in. Event-time (`ts`) ordering is
    the *page-load* question ("show me the latest N by when they
    happened"), not the *catch-up* question ("give me every row after
    this point, exactly once").

    `has_more` is computed honestly by fetching `limit + 1` rows and
    trimming the extra one off, never guessed from `len(events) == limit`
    (which is also true, and wrongly so, on the exact last page).
    """
    ascending = since is not None
    order = (Event.id.asc(),) if ascending else (Event.ts.desc(), Event.id.desc())
    stmt = select(Event).order_by(*order).limit(limit + 1)
    if since is not None:
        stmt = stmt.where(Event.id > since)

    with scope() as session:
        rows = session.execute(stmt).scalars().all()
        has_more = len(rows) > limit
        events = [EventOut.model_validate(row) for row in rows[:limit]]
    return EventsResponse(events=events, has_more=has_more)


# ---------------------------------------------------------------------------
# GET /api/alerts
# ---------------------------------------------------------------------------


@router.get("/api/alerts", response_model=AlertsResponse)
def list_alerts(
    acknowledged: bool | None = Query(
        default=None, description="Filter by acknowledgement state; omitted returns both."
    ),
    limit: int = Query(
        default=BACKEND_SETTINGS.api_alerts_default_limit,
        ge=1,
        le=BACKEND_SETTINGS.api_events_max_limit,
        description="Max rows to return. Above the cap -> 422 (never silently clamped).",
    ),
    scope: Callable[[], ContextManager[Session]] = Depends(get_session_scope),
) -> AlertsResponse:
    """Alert list, `ORDER BY ts DESC, id DESC` (same tie-break rationale as
    `/api/events` — see docs/PHASE5_TICKET8_PLAN.md section 8), using the
    existing `ix_alerts_acknowledged_ts_desc` index when `acknowledged` is
    supplied.
    """
    stmt = select(Alert).order_by(Alert.ts.desc(), Alert.id.desc()).limit(limit)
    if acknowledged is not None:
        stmt = stmt.where(Alert.acknowledged == acknowledged)

    with scope() as session:
        rows = session.execute(stmt).scalars().all()
        alerts = [AlertOut.model_validate(row) for row in rows]
    return AlertsResponse(alerts=alerts)


# ---------------------------------------------------------------------------
# POST /api/alerts/{id}/ack
# ---------------------------------------------------------------------------


@router.post("/api/alerts/{alert_id}/ack", response_model=AlertOut)
def acknowledge_alert(
    alert_id: int,
    scope: Callable[[], ContextManager[Session]] = Depends(get_session_scope),
) -> AlertOut:
    """Acknowledge an alert. 404 if it does not exist. Idempotent: acking
    an already-acknowledged alert leaves `acknowledged_at` untouched — the
    FIRST acknowledgement is the operator record, and overwriting it on a
    repeat click would erase who-noticed-it-when.
    """
    with scope() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
        if not alert.acknowledged:
            alert.acknowledged = True
            alert.acknowledged_at = datetime.now(timezone.utc)
        return AlertOut.model_validate(alert)


# ---------------------------------------------------------------------------
# GET /api/cii/{asset}
# ---------------------------------------------------------------------------


@router.get("/api/cii/{asset}", response_model=CiiResponse)
def get_cii(
    asset: str,
    anomaly_score: float = Query(
        default=1.0,
        gt=0.0,
        le=1.0,
        description="'If this asset were fully compromised' by default (1.0) — the on-demand question an operator asks.",
    ),
) -> CiiResponse:
    """On-demand blast radius for `asset`. 404 (never a fabricated
    all-zero 200) if `asset` is not a node in `build_graph()` — see
    decision D8-3: CLAUDE.md section 7 records the dashboard's What-If
    selectbox reproducing exactly this bug (offering assets that return an
    empty, all-zero `CIIResult()`), and this route must not repeat it.
    Membership is tested the same way Ticket #7 established (P5-17):
    presence in `build_criticality_map()`, which is keyed by exactly the
    graph's node set (`build_criticality_map -> compute_seed_rows ->
    build_graph`), so membership in it IS graph membership. No second
    criticality map is built here (Invariant D) — `build_criticality_map`
    is imported from `backend.ingest`, not re-derived.
    """
    criticality_map = build_criticality_map()
    if asset not in criticality_map:
        raise HTTPException(
            status_code=404,
            detail=f"asset {asset!r} is not a node in the dependency graph",
        )

    result = compute_cascading_impact_full(
        anomalous_asset_name=asset,
        anomaly_score=anomaly_score,
        criticality_map=criticality_map,
    )
    return CiiResponse(
        origin_asset=asset,
        anomaly_score=anomaly_score,
        cii_median=result.cii_median,
        cii_p5=result.cii_p5,
        cii_p95=result.cii_p95,
        impacted_assets=list(result.impacted_assets),
        hop_details=result.hop_details,
    )


# ---------------------------------------------------------------------------
# POST /api/replay/start
# ---------------------------------------------------------------------------


@router.post("/api/replay/start", response_model=ReplayStatusResponse)
def start_replay(
    body: ReplayStartRequest,
    runtime: AppRuntime = Depends(get_runtime),
) -> ReplayStatusResponse:
    """Start replaying `body.dataset` (-> `ReplayEngine.start()`'s `day`
    parameter, see `ReplayStartRequest`'s docstring) at `body.speed`. 503
    if the scorer never loaded (`_require_replay_engine`). `start()`
    raises `ReplayEngineError` when already running — that is 409
    Conflict, deliberately not a silent no-op: a caller that wanted a
    different day/speed must never mistake an ignored `start()` call for
    one that took effect (see `ReplayEngine.start()`'s own docstring).
    """
    engine = _require_replay_engine(runtime)
    try:
        engine.start(
            day=body.dataset,
            speed=body.speed,
            start_at=body.start_at,
            limit=body.limit,
        )
    except ReplayEngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReplayStatusResponse.from_status(engine.status())


# ---------------------------------------------------------------------------
# POST /api/replay/stop
# ---------------------------------------------------------------------------


@router.post("/api/replay/stop", response_model=ReplayStatusResponse)
def stop_replay(runtime: AppRuntime = Depends(get_runtime)) -> ReplayStatusResponse:
    """Idempotent — 200 even when not running, and even when the scorer
    never loaded (there is nothing to stop either way, and reporting 503
    for a no-op stop would be misleading). Returns the final status.
    """
    if runtime.engine is None:
        return ReplayStatusResponse.from_status(_NO_ENGINE_STATUS)
    runtime.engine.stop()
    return ReplayStatusResponse.from_status(runtime.engine.status())


# ---------------------------------------------------------------------------
# POST /api/replay/speed
# ---------------------------------------------------------------------------


@router.post("/api/replay/speed", response_model=ReplayStatusResponse)
def set_replay_speed(
    body: ReplaySpeedRequest,
    runtime: AppRuntime = Depends(get_runtime),
) -> ReplayStatusResponse:
    """Change replay speed mid-run. `multiplier` is validated `gt=0` by
    Pydantic (`ReplaySpeedRequest`), so 0 or a negative value is 422
    before this body ever runs, before the engine is ever touched. 409 if
    not currently running (`ReplayEngine.set_speed()` re-anchors a live
    schedule; there is no live schedule to re-anchor otherwise). 503 if
    the scorer never loaded.
    """
    engine = _require_replay_engine(runtime)
    if not engine.status().running:
        raise HTTPException(status_code=409, detail="replay is not running")
    engine.set_speed(body.multiplier)
    return ReplayStatusResponse.from_status(engine.status())
