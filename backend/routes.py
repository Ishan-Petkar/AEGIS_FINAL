"""
backend/routes.py — Phase 5 Ticket #8 (nine REST routes) + Ticket #13
(POST /api/inject, GET /api/inject/scenarios).

    GET  /api/health
    GET  /api/topology
    GET  /api/events?limit=&since=
    GET  /api/alerts?acknowledged=
    POST /api/alerts/{id}/ack
    GET  /api/cii/{asset}
    POST /api/replay/start
    POST /api/replay/stop
    POST /api/replay/speed
    WS   /ws/stream           (Ticket #9)
    GET  /api/inject/scenarios  (Ticket #13)
    POST /api/inject            (Ticket #13)
    GET  /api/stats              (Ticket #16)

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

import logging
from datetime import datetime, timezone
from typing import Callable, ContextManager

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.config import BACKEND_SETTINGS
from backend.db import session_scope
from backend.inject import SCENARIOS, InjectionError, build_injection_flows
from backend.ingest import (
    DETECTOR_HYBRID,
    DETECTOR_TRIPWIRE,
    DETECTOR_VOLUMETRIC,
    build_criticality_map,
    compute_risk_index,
)
from backend.ips.contracts import ACTIVE_PREVENTION_ACTIONS, ActionStatus
from backend.models import Alert, Event, EventScore, IpsAction
from backend.replay_engine import ReplayEngineError, ReplayStatus
from backend.runtime import AppRuntime
from backend.security import enforce_rate_limit, require_api_token
from backend.schemas import (
    AlertCountersOut,
    AlertOut,
    AlertsResponse,
    AlertSeverityCount,
    CiiResponse,
    DEFAULT_INJECT_TARGET_ASSET,
    EventOut,
    EventsResponse,
    HealthResponse,
    IngestCountersOut,
    InjectRequest,
    InjectResponse,
    IpsActionOut,
    IpsActionsResponse,
    IpsPolicyResponse,
    IpsRollbackRequest,
    ReplaySpeedRequest,
    ReplayStartRequest,
    ReplayStatusResponse,
    ScenarioOut,
    ScenariosResponse,
    StatsResponse,
    TopologyEdge,
    TopologyNode,
    TopologyResponse,
)
from backend.seed import compute_seed_rows
from cii_calculator import compute_cascading_impact_full
from config import SMART_CITY_ASSETS
from graph_manager import build_graph

logger = logging.getLogger(__name__)

router = APIRouter()

#: Applied to every state-changing route (Phase B improvement pass) —
#: `require_api_token` alone is a no-op while `BACKEND_SETTINGS.api_token`
#: is unset (the default), so this is inert for the loopback-bound local
#: demo and only starts actually gating requests once an operator sets
#: that env var. `enforce_rate_limit` is always active. See
#: `backend/security.py`'s module docstring for the honest security-model
#: caveat on the token check.
_MUTATING_ROUTE_DEPS = [Depends(require_api_token), Depends(enforce_rate_limit)]

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
    # Sector passthrough (docs/PHASE5_CONSOLE_REDESIGN_PLAN.md §3): read
    # directly off config.SMART_CITY_ASSETS rather than threading a new key
    # through compute_seed_rows()/Asset — that function's dicts are also
    # passed straight into the SQLAlchemy `Asset(**row)` constructor
    # (backend.seed.seed_assets), which has no `sector` column, so adding
    # it there would break asset seeding. This keeps the one permitted
    # backend touch confined to this route.
    sector_by_name = {a["asset_name"]: a.get("sector") for a in SMART_CITY_ASSETS}

    nodes = [
        TopologyNode(
            name=name,
            criticality=float(meta_by_name[name]["criticality"]),
            type=meta_by_name[name]["type"],
            purdue_level=meta_by_name[name]["purdue_level"],
            is_gateway=bool(meta_by_name[name]["is_gateway"]),
            sector=sector_by_name.get(name),
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

    Each row is enriched with its `event_scores` verdicts (`raw_score`,
    `calibrated_score`, `is_anomaly`, `confidence`, `tripwire_fired`) — see
    `EventOut`'s docstring — added so a WS client that missed live
    envelopes during a disconnect (Phase A improvement pass, "Backfill
    missed WebSocket events on reconnect") can call this route with
    `since=<last event id it saw>` and render the backfilled rows with the
    same fidelity as a live "event" envelope, instead of a degraded
    partial one.
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

        event_ids = [e.id for e in events]
        if event_ids:
            score_rows = (
                session.execute(
                    select(EventScore).where(EventScore.event_id.in_(event_ids))
                )
                .scalars()
                .all()
            )
            volumetric_by_event_id: dict[int, EventScore] = {}
            tripwire_event_ids: set[int] = set()
            hybrid_by_event_id: dict[int, EventScore] = {}
            fired_by_event_id: dict[int, list[str]] = {}
            for score_row in score_rows:
                if score_row.detector == DETECTOR_VOLUMETRIC:
                    volumetric_by_event_id[score_row.event_id] = score_row
                elif score_row.detector == DETECTOR_TRIPWIRE:
                    tripwire_event_ids.add(score_row.event_id)
                elif score_row.detector == DETECTOR_HYBRID:
                    hybrid_by_event_id[score_row.event_id] = score_row
                # Every persisted channel's own fired state (volumetric
                # and tripwire included) — a REST-side reconstruction of
                # `FusedDecision.fired_detectors`, which the live
                # `/ws/stream` "hybrid" envelope already carries. The
                # `DETECTOR_HYBRID` row is excluded: its `is_anomaly`
                # means "band != BENIGN" for the FUSED decision, not "this
                # channel fired", so including it here would misrepresent
                # it as a seventh contributing detector.
                if score_row.detector != DETECTOR_HYBRID and score_row.is_anomaly:
                    fired_by_event_id.setdefault(score_row.event_id, []).append(
                        score_row.detector
                    )
            for event in events:
                volumetric = volumetric_by_event_id.get(event.id)
                if volumetric is not None:
                    event.raw_score = volumetric.raw_score
                    event.calibrated_score = volumetric.calibrated_score
                    event.is_anomaly = volumetric.is_anomaly
                    event.confidence = volumetric.confidence
                event.tripwire_fired = event.id in tripwire_event_ids
                hybrid = hybrid_by_event_id.get(event.id)
                if hybrid is not None:
                    event.hybrid_threat_score = hybrid.calibrated_score
                event.fired_detectors = fired_by_event_id.get(event.id, [])

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


@router.post(
    "/api/alerts/{alert_id}/ack",
    response_model=AlertOut,
    dependencies=_MUTATING_ROUTE_DEPS,
)
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
# GET /api/ips/policy | GET /api/ips/actions | POST /api/ips/actions/{id}/rollback
#
# The IPS (prevention) layer's own routes (backend/ips/). Mirrors the
# alerts routes immediately above wherever the shape matches (list +
# filter, mutating-route deps, 404 on an unknown id) — deliberately not a
# separate pattern.
# ---------------------------------------------------------------------------


@router.get("/api/ips/policy", response_model=IpsPolicyResponse)
def get_ips_policy() -> IpsPolicyResponse:
    """The currently configured IPS thresholds — read straight from
    `BACKEND_SETTINGS.ips_*`, never a second copy of these numbers. No DB,
    no runtime/engine required (unlike the routes below), since this is
    pure static configuration — same shape as `GET /api/inject/scenarios`.
    """
    s = BACKEND_SETTINGS
    return IpsPolicyResponse(
        enabled=s.ips_enabled,
        dry_run=s.ips_dry_run,
        min_corroborating_detectors=s.ips_min_corroborating_detectors,
        rate_limit_min_threat_score=s.ips_rate_limit_min_threat_score,
        block_min_threat_score=s.ips_block_min_threat_score,
        block_min_asset_criticality=s.ips_block_min_asset_criticality,
        quarantine_min_asset_criticality=s.ips_quarantine_min_asset_criticality,
        quarantine_min_cii_median=s.ips_quarantine_min_cii_median,
        rate_limit_ttl_sec=s.ips_rate_limit_ttl_sec,
        block_ttl_sec=s.ips_block_ttl_sec,
        quarantine_ttl_sec=s.ips_quarantine_ttl_sec,
    )


@router.get("/api/ips/actions", response_model=IpsActionsResponse)
def list_ips_actions(
    active: bool | None = Query(
        default=None,
        description=(
            "True: only currently-active PREVENTION rows — action is "
            "rate_limit/block/quarantine (not a bare 'alert' decision, "
            "which is never enforced and has nothing to be 'active') AND "
            "status is still simulated/enforced. False: everything else "
            "(terminal rows, plus every 'alert'-only decision). Omitted "
            "returns both."
        ),
    ),
    target_asset: str | None = Query(
        default=None, description="Filter to one target asset."
    ),
    limit: int = Query(
        default=BACKEND_SETTINGS.api_alerts_default_limit,
        ge=1,
        le=BACKEND_SETTINGS.api_events_max_limit,
        description="Max rows to return. Above the cap -> 422 (never silently clamped).",
    ),
    scope: Callable[[], ContextManager[Session]] = Depends(get_session_scope),
) -> IpsActionsResponse:
    """Action list, `ORDER BY ts DESC, id DESC` (same tie-break rationale
    as `/api/events`/`/api/alerts`) — the requirement's "action history".
    """
    stmt = select(IpsAction).order_by(IpsAction.ts.desc(), IpsAction.id.desc()).limit(limit)
    if target_asset is not None:
        stmt = stmt.where(IpsAction.target_asset == target_asset)
    active_action_values = tuple(a.value for a in ACTIVE_PREVENTION_ACTIONS)
    active_status_values = (ActionStatus.SIMULATED.value, ActionStatus.ENFORCED.value)
    if active is True:
        stmt = stmt.where(
            IpsAction.action.in_(active_action_values),
            IpsAction.status.in_(active_status_values),
        )
    elif active is False:
        stmt = stmt.where(
            ~(
                IpsAction.action.in_(active_action_values)
                & IpsAction.status.in_(active_status_values)
            )
        )

    with scope() as session:
        rows = session.execute(stmt).scalars().all()
        actions = [IpsActionOut.model_validate(row) for row in rows]
    return IpsActionsResponse(actions=actions)


@router.post(
    "/api/ips/actions/{action_id}/rollback",
    response_model=IpsActionOut,
    dependencies=_MUTATING_ROUTE_DEPS,
)
def rollback_ips_action(
    action_id: int,
    body: IpsRollbackRequest = IpsRollbackRequest(),
    runtime: AppRuntime = Depends(get_runtime),
    scope: Callable[[], ContextManager[Session]] = Depends(get_session_scope),
) -> IpsActionOut:
    """Manually roll back / unblock an active IPS action — the
    requirement's "unblock/rollback" control.

    503 if the scorer never loaded (`_require_replay_engine`, mirroring
    every other route that needs a live `IngestPipeline` — `pipeline` and
    `engine` are set or left `None` together, see `AppRuntime`'s
    docstring). 404 if the id does not exist. 409 if it exists but is
    already in a terminal state (already rolled back / expired /
    superseded / failed) — rolling back something not currently active is
    not a legitimate no-op the way re-acking an alert is; it means the
    operator's view of what's blocked disagrees with the system's, which
    is worth surfacing.

    Delegates the actual state change to `IngestPipeline.
    rollback_ips_action` (not duplicated here) so the SAME code path
    updates the active-mitigation registry, calls the enforcement
    adapter's `rollback()`, and builds the envelope, regardless of
    whether the rollback was triggered by this route or by TTL expiry
    inside `ingest_batch` — one rollback implementation, two triggers.
    """
    _require_replay_engine(runtime)  # asserts a live pipeline exists — see docstring
    envelope = runtime.pipeline.rollback_ips_action(action_id, reason=body.reason)
    if envelope is None:
        with scope() as session:
            row = session.get(IpsAction, action_id)
            # Read while still attached to `session` -- `row` itself must
            # not be touched again once this block exits (same
            # DetachedInstanceError risk `IngestPipeline.
            # rollback_ips_action` documents at its own equivalent point).
            status = row.status if row is not None else None
        if row is None:
            raise HTTPException(status_code=404, detail=f"ips action {action_id} not found")
        raise HTTPException(
            status_code=409,
            detail=(
                f"ips action {action_id} is not active (status={status!r}); "
                "nothing to roll back"
            ),
        )
    runtime.pipeline.publish_envelope(envelope)
    with scope() as session:
        row = session.get(IpsAction, action_id)
        return IpsActionOut.model_validate(row)


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


@router.post(
    "/api/replay/start",
    response_model=ReplayStatusResponse,
    dependencies=_MUTATING_ROUTE_DEPS,
)
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


@router.post(
    "/api/replay/stop",
    response_model=ReplayStatusResponse,
    dependencies=_MUTATING_ROUTE_DEPS,
)
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


@router.post(
    "/api/replay/speed",
    response_model=ReplayStatusResponse,
    dependencies=_MUTATING_ROUTE_DEPS,
)
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


# ---------------------------------------------------------------------------
# GET /api/inject/scenarios | POST /api/inject (Ticket #13)
# ---------------------------------------------------------------------------


@router.get("/api/inject/scenarios", response_model=ScenariosResponse)
def list_inject_scenarios() -> ScenariosResponse:
    """The real-attack scenario registry (`backend.inject.SCENARIOS`), so
    the UI lists scenarios rather than hardcoding them. Every entry
    replays real, labelled CIC-IDS2017 attack traffic — see
    `backend/inject.py`'s module docstring for the verified per-label
    flow counts. No DB, no scorer, no engine required.
    """
    return ScenariosResponse(
        scenarios=[
            ScenarioOut(
                name=spec.name,
                day=spec.day,
                label=spec.label,
                is_honeytoken=spec.is_honeytoken,
                description=spec.description,
            )
            for spec in SCENARIOS.values()
        ]
    )


@router.post(
    "/api/inject",
    response_model=InjectResponse,
    dependencies=_MUTATING_ROUTE_DEPS,
)
def post_inject(
    body: InjectRequest,
    runtime: AppRuntime = Depends(get_runtime),
) -> InjectResponse:
    """Replay REAL captured attack flows (never fabricated —
    `src/data_generator.generate_scripted_attack()` is never called here),
    re-targeted at an operator-chosen curated asset (decision D13-1), via
    the EXISTING `ReplayEngine.inject()` — no second injection path.

    503 if the scorer never loaded (`_require_replay_engine`, mirroring
    every other replay-control route).

    409 if no replay is currently running. This is NOT a cosmetic choice:
    `ReplayEngine.inject()` only queues flows into `_injection_buffer`,
    which is drained exclusively inside `_tick_once()`, which only runs
    from the engine's background thread while `start()` has it running.
    `start()` itself unconditionally clears any pending injection buffer
    before spawning that thread. So calling `inject()` while stopped does
    not raise, but the flows would sit queued and then be silently wiped
    out by the next `start()` — a silent no-op this route refuses to
    produce (docs/PHASE5_TICKET13_PLAN.md section 5: "if it does [require
    a started engine], say so explicitly rather than silently no-oping").
    An operator must `POST /api/replay/start` first.

    422 for an unknown `scenario`, or a `target_asset` that is not a
    curated asset with a real static IP identifier (stricter than plain
    `build_criticality_map()` membership — see `backend.inject.
    resolvable_target_assets()`'s docstring for why a gateway/synthesized
    node cannot be a valid target here).

    `count` above `BACKEND_SETTINGS.inject_max_flows` is already 422'd by
    `InjectRequest`'s own Pydantic bound before this body runs.
    """
    engine = _require_replay_engine(runtime)
    status = engine.status()
    if not status.running:
        raise HTTPException(
            status_code=409,
            detail=(
                "no replay session is running; POST /api/inject requires "
                "an active replay (start one via POST /api/replay/start) "
                "because ReplayEngine.inject() only drains its queue on "
                "the engine's next scheduling tick — injecting into a "
                "stopped engine would silently do nothing."
            ),
        )

    target_asset = body.target_asset or DEFAULT_INJECT_TARGET_ASSET
    try:
        flows = build_injection_flows(body.scenario, target_asset, body.count)
    except InjectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        engine.inject(flows)
    except ReplayEngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    spec = SCENARIOS[body.scenario]
    return InjectResponse(
        scenario=body.scenario,
        target_asset=target_asset,
        flows_injected=len(flows),
        real_label=spec.label,
        is_honeytoken=spec.is_honeytoken,
        message=(
            f"What-if injected: {len(flows)} REAL captured "
            f"{spec.label!r} flows ({spec.day}) re-targeted at "
            f"{target_asset!r}. This is a what-if scenario, not observed "
            "capture traffic -- every injected event is persisted with "
            "batch_origin='injected'."
            + (
                " AEGIS's own planted honeytoken credential flag was "
                "additionally set on these real flows (decision D13-2); "
                "a honeytoken touch cannot exist in the 2017 public "
                "capture itself."
                if spec.is_honeytoken
                else ""
            )
        ),
        replay_session_id=status.replay_session_id,
    )


# ---------------------------------------------------------------------------
# GET /api/stats (Ticket #16)
# ---------------------------------------------------------------------------


@router.get("/api/stats", response_model=StatsResponse)
def get_stats(
    runtime: AppRuntime = Depends(get_runtime),
    scope: Callable[[], ContextManager[Session]] = Depends(get_session_scope),
) -> StatsResponse:
    """The header counters (docs/PHASE5_BUILD_PLAN.md section 7:
    "events/s, alerts, risk index"). Composed from THREE independently
    real sources -- see `StatsResponse`'s docstring for why they stay
    distinguishable rather than being flattened together.

    503 if the scorer never loaded (`_require_replay_engine`, mirroring
    every other replay-control route, docs/PHASE5_TICKET16_PLAN.md
    section 2) -- there is no `IngestPipeline` to read counters from in
    that state, and reporting fabricated zeros instead would misrepresent
    "never started" as "ran and saw nothing".

    Alert counts come from the DATABASE via two bounded `GROUP BY`
    aggregates (never the in-memory `IngestStats.alerts_created` counter,
    and never a per-row fetch): one grouped by `(severity, acknowledged)`
    for the totals below, and one grouped by `(severity, asset)` -- WHERE
    acknowledged is false -- feeding `compute_risk_index`. Aggregating in
    Postgres means the result set is bounded by the number of DISTINCT
    (severity, asset) combinations an operator has left outstanding, not
    by how many alert rows the table has ever accumulated; `.limit()` is
    applied anyway as a hard backstop, matching every other paginated
    route in this module.

    Using the database (not the in-memory counter) is what makes this
    survive a process restart -- an operator restarting the backend mid-
    incident must still see the alerts that already happened, not a
    reset-to-zero panel.
    """
    engine = _require_replay_engine(runtime)
    ingest_stats = runtime.pipeline.stats()

    with scope() as session:
        severity_ack_rows = session.execute(
            select(Alert.severity, Alert.acknowledged, func.count())
            .group_by(Alert.severity, Alert.acknowledged)
            .limit(BACKEND_SETTINGS.api_events_max_limit)
        ).all()
        unacknowledged_rows = session.execute(
            select(Alert.severity, Alert.asset, func.count())
            .where(Alert.acknowledged.is_(False))
            .group_by(Alert.severity, Alert.asset)
            .limit(BACKEND_SETTINGS.api_events_max_limit)
        ).all()

    alerts_total = 0
    alerts_unacknowledged = 0
    by_severity: dict[str, dict[str, int]] = {}
    for severity, acknowledged, count in severity_ack_rows:
        count = int(count)
        alerts_total += count
        bucket = by_severity.setdefault(severity, {"acknowledged": 0, "unacknowledged": 0})
        if acknowledged:
            bucket["acknowledged"] += count
        else:
            bucket["unacknowledged"] += count
            alerts_unacknowledged += count

    criticality_map = build_criticality_map()
    risk_index = compute_risk_index(
        [(severity, asset, int(count)) for severity, asset, count in unacknowledged_rows],
        criticality_map,
    )

    return StatsResponse(
        ingest=IngestCountersOut(**vars(ingest_stats)),
        replay=ReplayStatusResponse.from_status(engine.status()),
        alerts=AlertCountersOut(
            total=alerts_total,
            unacknowledged=alerts_unacknowledged,
            by_severity=[
                AlertSeverityCount(
                    severity=severity,
                    acknowledged=bucket["acknowledged"],
                    unacknowledged=bucket["unacknowledged"],
                )
                for severity, bucket in sorted(by_severity.items())
            ],
        ),
        risk_index=risk_index,
    )


# ---------------------------------------------------------------------------
# WS /ws/stream (Ticket #9)
# ---------------------------------------------------------------------------


def get_runtime_ws(websocket: WebSocket) -> AppRuntime:
    """Websocket-scope twin of `get_runtime` (above). Kept separate rather
    than reused because FastAPI resolves a `Request`-typed dependency
    parameter against the HTTP connection scope; `WS /ws/stream` runs in
    the websocket scope, so its dependency must be annotated `WebSocket`
    instead. Tests override THIS callable (mirroring how test_api.py
    overrides `get_runtime`) to hand the route a fake `AppRuntime` without
    exercising the real lifespan.
    """
    return websocket.app.state.runtime


@router.websocket("/ws/stream")
async def ws_stream(
    websocket: WebSocket,
    runtime: AppRuntime = Depends(get_runtime_ws),
) -> None:
    """The real live feed. Transport only -- see `backend/ws_broadcaster.py`
    for the D9-1 (cross-thread publish) and D9-2 (per-client backpressure)
    machinery this route merely drives.

    On connect: register with `runtime.broadcaster`, which accepts the
    socket, starts this connection's writer task, and sends an immediate
    `{"type": "hello", "data": <ReplayStatusResponse>}` frame from
    `runtime.engine.status()` (or an all-idle status if the scorer never
    loaded and there is no engine at all -- `WS /ws/stream` still accepts
    connections in that case, same "still starts, still readable" posture
    as the read-only REST routes).

    This route does not read anything the client sends -- `WS /ws/stream`
    is one-directional (server -> client) by design (docs/
    PHASE5_TICKET9_PLAN.md). `receive_text()` is used purely to block until
    `WebSocketDisconnect`, which is how Starlette signals the client went
    away; any other exception during that wait is treated the same way, so
    a single misbehaving client is always cleanly unregistered and never
    propagates to another connection or kills the endpoint.
    """
    status = runtime.engine.status() if runtime.engine is not None else _NO_ENGINE_STATUS
    hello_data = ReplayStatusResponse.from_status(status).model_dump(mode="json")
    client = await runtime.broadcaster.register(websocket, hello_data)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.warning("ws_stream: client %d ended abnormally", client.client_id, exc_info=True)
    finally:
        await runtime.broadcaster.unregister(client)
