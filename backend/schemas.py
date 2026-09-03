"""
backend/schemas.py — Phase 5 Ticket #8: Pydantic request/response models
for the nine REST routes in `backend/routes.py`.

Response models that wrap a SQLAlchemy ORM row (`EventOut`, `AlertOut`) use
`model_config = ConfigDict(from_attributes=True)` and are constructed via
`.model_validate(row)` **inside the route handler**, before the request's
`session_scope` context manager exits — this copies every field out to a
plain Pydantic instance while the ORM object is still attached to a live
session, so nothing downstream (response serialization, which runs after
the route handler returns) can trip a `DetachedInstanceError`.

`ReplayStatusResponse.from_status()` similarly adapts
`backend.replay_engine.ReplayStatus` (a plain frozen dataclass, not a
Pydantic model) into the wire schema — kept as an explicit classmethod
rather than `from_attributes=True` because `ReplayStatus.speed`/`.day` can
legitimately be `None` (engine never started) and the field-by-field
mapping makes that visible rather than relying on structural coincidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.config import BACKEND_SETTINGS

# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """See docs/PHASE5_TICKET8_PLAN.md section 8. `status` is driven SOLELY
    by the database check (a cheap `SELECT 1`) — `scorer_loaded` and
    `replay_running` are informational booleans alongside it, not inputs to
    `status` themselves, so a missing model artifact does not by itself
    turn a working, DB-backed deployment "degraded".

    `scorer_load_error` (LOW-1, review fix): `AppRuntime.scorer_load_error`
    already captured *why* `StreamingScorer.load()` failed
    (`backend/runtime.py`), but the original response threw it away and
    exposed only the boolean `scorer_loaded=False` — an operator staring at
    the console on demo day sees the flag but has nothing to act on. This
    field carries the same message `_require_replay_engine`'s 503 already
    quotes, so `/api/health` alone is enough to diagnose it (e.g. "run
    `python -m backend.warmup`") without provoking a replay-control 503
    just to read the reason. `None` whenever `scorer_loaded` is `True`.
    """

    status: Literal["ok", "degraded"]
    database: bool
    scorer_loaded: bool
    scorer_load_error: Optional[str]
    replay_running: bool
    uptime_sec: float


# ---------------------------------------------------------------------------
# GET /api/topology
# ---------------------------------------------------------------------------


class TopologyNode(BaseModel):
    name: str
    criticality: float
    type: Optional[str]
    purdue_level: Optional[int]
    is_gateway: bool
    #: Console redesign (docs/PHASE5_CONSOLE_REDESIGN_PLAN.md §3, the one
    #: permitted backend touch): passthrough of config.SMART_CITY_ASSETS'
    #: `sector` field so the frontend derives sector-view aggregation from
    #: real data instead of a hardcoded name list. `None` for gateway and
    #: synthesized nodes (City_Grid), which are not curated assets and are
    #: not owned by any one sector.
    sector: Optional[str] = None


class TopologyEdge(BaseModel):
    source: str
    target: str
    edge_type: str
    prob: float
    is_gateway_edge: bool


class TopologyResponse(BaseModel):
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]


# ---------------------------------------------------------------------------
# GET /api/events
# ---------------------------------------------------------------------------


class EventOut(BaseModel):
    """`raw_score`/`calibrated_score`/`is_anomaly`/`confidence`/
    `tripwire_fired` (Phase A improvement pass, roadmap "Backfill missed
    WebSocket events on reconnect") are NOT columns on `Event` — they are
    enriched in `list_events()` from a follow-up `event_scores` query, so
    a REST-fetched event carries the same detector verdicts a live `WS
    /ws/stream` "event" envelope does. `EventOut.model_validate(row)`
    leaves them at their declared defaults (`None`/`False`) because a
    plain `Event` ORM row has no such attributes; the route then
    overwrites them once the scores are known. Never fabricated: a `None`
    score means "no volumetric score row was found for this event" (would
    indicate a real data gap), not "score is zero" — and `tripwire_fired`
    defaults to `False` only because that default is actually correct
    on its own terms (no `event_scores` row with `detector="tripwire"`
    for this event IS the unambiguous "did not fire" case, per
    `IngestPipeline._persist_scores`'s own docstring: a tripwire row is
    written only where it fired).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    observed_at: Optional[datetime]
    ingested_at: datetime
    source_id: Optional[str]
    destination_id: Optional[str]
    source_asset: Optional[str]
    destination_asset: Optional[str]
    protocol: Optional[str]
    bytes: Optional[int]
    packets: Optional[int]
    duration_sec: Optional[float]
    signal_type: str
    source_dataset: Optional[str]
    timing_provenance: str
    replay_session_id: UUID
    source_row_id: str
    raw: Optional[dict[str, Any]]
    raw_score: Optional[float] = None
    calibrated_score: Optional[float] = None
    is_anomaly: Optional[bool] = None
    confidence: Optional[float] = None
    tripwire_fired: bool = False


class EventsResponse(BaseModel):
    """`has_more` (HIGH-1, review fix): `True` when the query matched more
    rows than `limit` allowed through — i.e. the caller is behind and
    should poll again with an advanced `since`. Computed honestly by
    fetching `limit + 1` rows and trimming to `limit`, never hardcoded or
    inferred from `len(events) == limit` (which would also be true on the
    exact last page and give a false positive). See `since`'s description
    on `GET /api/events` (`backend/routes.py`) for why the response
    envelope needs this at all: without it, a client that has fallen behind
    has no way to distinguish "caught up" from "still behind" once
    `since`-with-DESC's old silent data loss (docs/PHASE5_STATE.md decision
    P5-18) is fixed to never happen — but it still needs telling *when* to
    stop polling.
    """

    events: list[EventOut]
    has_more: bool


# ---------------------------------------------------------------------------
# GET /api/alerts, POST /api/alerts/{id}/ack
# ---------------------------------------------------------------------------


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    severity: str
    asset: str
    title: str
    detail: Optional[str]
    explanation: Optional[dict[str, Any]]
    cii_snapshot_id: Optional[int]
    acknowledged: bool
    acknowledged_at: Optional[datetime]


class AlertsResponse(BaseModel):
    alerts: list[AlertOut]


# ---------------------------------------------------------------------------
# GET /api/cii/{asset}
# ---------------------------------------------------------------------------


class CiiResponse(BaseModel):
    origin_asset: str
    anomaly_score: float
    cii_median: float
    cii_p5: float
    cii_p95: float
    impacted_assets: list[str]
    hop_details: dict[str, dict[str, float]]


# ---------------------------------------------------------------------------
# POST /api/replay/start | stop | speed
# ---------------------------------------------------------------------------


class ReplayStartRequest(BaseModel):
    """`dataset` maps onto `ReplayEngine.start()`'s `day` parameter — the
    wire contract (docs/PHASE5_BUILD_PLAN.md section 7) and the engine
    (Ticket #6) disagree on the name; the contract wins at this boundary
    (docs/PHASE5_TICKET8_PLAN.md section 8). Every field is optional: a
    `None` falls back to the engine's own `BACKEND_SETTINGS`-derived
    default for that parameter.
    """

    dataset: Optional[str] = Field(
        default=None,
        description="Capture day to replay, e.g. 'friday-morning'. "
        "Defaults to BACKEND_SETTINGS.replay_default_dataset_day.",
    )
    speed: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Replay speed multiplier. Defaults to BACKEND_SETTINGS.replay_speed.",
    )
    start_at: Optional[datetime | str] = Field(
        default=None,
        description=(
            "Skip into the capture before replaying: either an 'HH:MM' "
            "string or a full ISO-8601 datetime. E.g. friday-morning's "
            "first real attack lands at 09:34, about 1.5 minutes into a "
            "20x replay from the top."
        ),
    )
    limit: Optional[int] = Field(
        default=None,
        ge=0,
        description="Cap on how many flows (from start_at onward) this run will ever emit.",
    )


class ReplaySpeedRequest(BaseModel):
    multiplier: float = Field(
        gt=0.0, description="New replay speed multiplier; must be > 0 (422 otherwise)."
    )


class ReplayStatusResponse(BaseModel):
    """Built from `ReplayEngine.status()` on every replay-control route
    (docs/PHASE5_TICKET8_PLAN.md section 8, last paragraph) so the client
    always sees authoritative engine state, not an echo of the request."""

    running: bool
    day: Optional[str]
    speed: Optional[float]
    replay_session_id: Optional[UUID]
    emitted_count: int
    total_for_day: int
    current_virtual_position: Optional[datetime]
    lag_seconds: float
    batches_emitted: int
    consumer_error_count: int
    consumer_failed_flow_count: int

    @classmethod
    def from_status(cls, status: Any) -> "ReplayStatusResponse":
        """Adapt a `backend.replay_engine.ReplayStatus` (frozen dataclass)."""
        return cls(
            running=status.running,
            day=status.day,
            speed=status.speed,
            replay_session_id=status.replay_session_id,
            emitted_count=status.emitted_count,
            total_for_day=status.total_for_day,
            current_virtual_position=status.current_virtual_position,
            lag_seconds=status.lag_seconds,
            batches_emitted=status.batches_emitted,
            consumer_error_count=status.consumer_error_count,
            consumer_failed_flow_count=status.consumer_failed_flow_count,
        )


# ---------------------------------------------------------------------------
# GET /api/inject/scenarios | POST /api/inject (Ticket #13)
# ---------------------------------------------------------------------------


class ScenarioOut(BaseModel):
    """One entry from `backend.inject.SCENARIOS` — real dataset day + real
    label, never a fabrication knob. `is_honeytoken` names the one
    scenario (decision D13-2) that additionally sets AEGIS's own
    deception-layer control flag on otherwise-real, unmodified flows."""

    name: str
    day: str
    label: str
    is_honeytoken: bool
    description: str


class ScenariosResponse(BaseModel):
    scenarios: list[ScenarioOut]


#: Curated default target — the smart-city payment gateway, the same
#: asset the ticket's own verification example names. Used only when the
#: caller omits `target_asset` entirely.
DEFAULT_INJECT_TARGET_ASSET = "City_Payment_Gateway"


class InjectRequest(BaseModel):
    """`scenario` is required and must be a key in `backend.inject.
    SCENARIOS` (422 otherwise). `target_asset` defaults to
    `DEFAULT_INJECT_TARGET_ASSET` and must be a CURATED asset with a real
    static IP identifier (422 otherwise — see `backend.inject.
    resolvable_target_assets()`, stricter than plain graph membership).
    `count` is bounded by `BACKEND_SETTINGS.inject_max_flows` at the
    Pydantic layer (422 above the cap, never silently clamped, mirroring
    `/api/events`'s `limit`).
    """

    scenario: str
    target_asset: Optional[str] = Field(
        default=None,
        description=f"Curated asset to compromise. Defaults to {DEFAULT_INJECT_TARGET_ASSET!r}.",
    )
    count: int = Field(
        default=100,
        ge=1,
        le=BACKEND_SETTINGS.inject_max_flows,
        description="Number of real flows to replay. Bounded by BACKEND_SETTINGS.inject_max_flows.",
    )


class InjectResponse(BaseModel):
    """Explicitly names the scenario and states plainly that these are
    real capture flows re-targeted for a what-if — decision D13-1's
    "must be unmistakable" requirement. Never phrased as observed
    telemetry."""

    scenario: str
    target_asset: str
    flows_injected: int
    real_label: str
    is_honeytoken: bool
    message: str
    replay_session_id: Optional[UUID]


# ---------------------------------------------------------------------------
# GET /api/stats (Ticket #16)
# ---------------------------------------------------------------------------


class IngestCountersOut(BaseModel):
    """Cumulative counters since process start -- field-for-field mirror of
    `backend.ingest.IngestStats` (see that dataclass's docstring for what
    each counter means). Process-lifetime only: reset by a backend
    restart, in memory only. Contrast with `AlertCountersOut` below, whose
    numbers come from the database and survive a restart.

    `alerts_suppressed` (decision D16-3) counts volumetric anomalies that
    were detected, scored, persisted, and broadcast but deliberately did
    NOT page an operator -- see `backend/ingest.py`'s "Alert policy"
    module-docstring section and `docs/DETECTION_STUDY.md` (~0.02
    precision on the volumetric channel). Surfacing it makes the alert
    policy visible instead of hidden: the system is saying "I saw N of
    these and chose not to wake you" rather than staying silent about the
    ones it filtered.
    """

    batches: int
    flows_received: int
    events_inserted: int
    events_deduplicated: int
    anomalies: int
    tripwire_hits: int
    cii_computed: int
    cii_reused: int
    alerts_created: int
    alerts_suppressed: int
    broadcast_failures: int
    events_pruned: int


class AlertSeverityCount(BaseModel):
    severity: str
    acknowledged: int
    unacknowledged: int


class AlertCountersOut(BaseModel):
    """Alert counts read from the `alerts` TABLE (never the in-memory
    `IngestStats.alerts_created` counter) -- so these survive a backend
    restart, unlike `IngestCountersOut` above. Computed via a bounded
    `GROUP BY severity, acknowledged` aggregate (`backend/routes.py`'s
    `get_stats`), never a per-row fetch.
    """

    total: int
    unacknowledged: int
    by_severity: list[AlertSeverityCount]


class StatsResponse(BaseModel):
    """GET /api/stats (Ticket #16) -- the header counters. Composed from
    THREE independently real sources, deliberately kept distinguishable
    rather than flattened into one ambiguous namespace (docs/
    PHASE5_TICKET16_PLAN.md section 2):

    - `ingest`  -- `IngestPipeline.stats()`, cumulative since process start.
    - `replay`  -- `ReplayEngine.status()`, the live snapshot (same shape
      every other replay-control route already returns).
    - `alerts`  -- real counts from the alerts table, survive a restart.
    - `risk_index` -- decision D16-1; see `backend.ingest.compute_risk_index`
      for the exact formula and why it is not built on CII. `0` (never
      `null`/omitted) when there are no unacknowledged alerts -- that is a
      real "nothing outstanding" state, not "no basis to compute".

    Deliberately does NOT return an `events/s` or any other rate field
    (decision D16-2): the frontend already computes a live per-second
    rate from what it actually received over the WebSocket
    (`useEventStream`'s `eventsPerSecond`), and a second server-side
    number under the same-looking name is exactly the
    two-numbers-that-can-disagree defect this project hit twice already
    (Ticket #3's header/panel contradiction, Ticket #10's duplicate
    sockets). A caller that wants a rate should derive one from two
    `ingest` snapshots' `events_inserted`, timestamped by the caller
    itself.
    """

    ingest: IngestCountersOut
    replay: ReplayStatusResponse
    alerts: AlertCountersOut
    risk_index: int
