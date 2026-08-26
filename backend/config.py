"""
backend/config.py — typed, validated configuration for the Phase 5 backend.

Mirrors the style of `src/settings.py`: every tunable is a Pydantic field
with bounds and a docstring, reachable via a frozen module-level singleton
(`BACKEND_SETTINGS` here, `SETTINGS` there). The difference is the source
of truth — `src/settings.py` governs *engine* tuning (decay rates,
thresholds, model hyperparameters) and never reads the environment;
`BackendSettings` governs *deployment* concerns (database connection, API
bind address, replay pacing, model artifact location) and is read from the
process environment (prefix `AEGIS_`) and an optional `.env` file, via
`pydantic-settings`.

Usage:
    from backend.config import BACKEND_SETTINGS
    print(BACKEND_SETTINGS.database_url)

Escape hatch: setting `AEGIS_DATABASE_URL` overrides the individual
`AEGIS_DB_*` component fields entirely — the conventional way to point a
deployment at a URL-based connection string (e.g. a managed Postgres
instance) without decomposing it into host/port/user/password/name.
"""

from pathlib import Path
from typing import ClassVar
from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root — the parent of this file's directory (backend/), derived the
# same deterministic way as backend/__init__.py's _SRC_DIR so it is
# independent of the process's current working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# BackendSettings
# ---------------------------------------------------------------------------


class BackendSettings(BaseSettings):
    """Deployment configuration for the Phase 5 backend (API + replay + DB).

    Every field is overridable via an `AEGIS_<FIELD_NAME>` environment
    variable or a `.env` file at the repo root (see `.env.example`).
    Defaults match the Postgres environment provisioned for local
    development (role `aegis` / password `aegis` / database `aegis` on
    `127.0.0.1:5432`).
    """

    model_config = SettingsConfigDict(
        env_prefix="AEGIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        frozen=True,
        extra="ignore",
    )

    # ---- Database: connection components -----------------------------
    db_host: str = Field(
        default="127.0.0.1",
        description="Postgres host for the backend's primary connection.",
    )
    db_port: int = Field(
        default=5432,
        ge=1,
        le=65_535,
        description="Postgres port.",
    )
    db_user: str = Field(
        default="aegis",
        description="Postgres role used by the backend.",
    )
    db_password: str = Field(
        default="aegis",
        description=(
            "Postgres role password. Local-dev default only — see "
            ".env.example for the production override convention."
        ),
    )
    db_name: str = Field(
        default="aegis",
        description="Postgres database name.",
    )
    db_url_override: str | None = Field(
        default=None,
        validation_alias="AEGIS_DATABASE_URL",
        description=(
            "Full SQLAlchemy connection URL. When set, this takes "
            "precedence over db_host/db_port/db_user/db_password/db_name "
            "entirely — the conventional single-variable deployment "
            "escape hatch. Leave unset to compose the URL from the "
            "component fields instead."
        ),
    )

    # ---- Database: connection pool -------------------------------------
    db_pool_size: int = Field(
        default=5,
        ge=1,
        le=100,
        description="SQLAlchemy engine connection pool size (persistent connections).",
    )
    db_max_overflow: int = Field(
        default=10,
        ge=0,
        le=100,
        description=(
            "Extra connections SQLAlchemy may open above db_pool_size "
            "under burst load before callers block."
        ),
    )
    db_pool_timeout_sec: float = Field(
        default=30.0,
        gt=0.0,
        le=300.0,
        description="Seconds to wait for a connection from the pool before raising.",
    )
    db_pool_pre_ping: bool = Field(
        default=True,
        description=(
            "Issue a lightweight SELECT before handing out a pooled "
            "connection, so a connection killed by a Postgres restart is "
            "detected and transparently replaced instead of surfacing a "
            "stale-connection error to the caller (DB-restart resilience "
            "requirement)."
        ),
    )

    # ---- API server ------------------------------------------------------
    api_host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address for the FastAPI/uvicorn server. Defaults to "
            "127.0.0.1 (loopback only) because the Phase 5 API and "
            "WebSocket have no authentication — replay control "
            "(POST /api/replay/start|stop|speed) and injection "
            "(POST /api/inject) are unauthenticated state-changing "
            "endpoints. Binding 0.0.0.0 is a deliberate opt-in for cases "
            "like container networking or demoing from another device on "
            "a trusted network, not a general-purpose default: on shared "
            "wifi (e.g. a hackathon venue) it hands every device on the "
            "LAN unauthenticated control of the demo."
        ),
    )
    api_port: int = Field(
        default=8000,
        ge=1,
        le=65_535,
        description=(
            "Bind port for the FastAPI/uvicorn server. Deliberately "
            "distinct from Streamlit's default 8501 so the Research "
            "Console and the Phase 5 backend can run side by side."
        ),
    )

    # ---- Replay ------------------------------------------------------
    replay_speed: float = Field(
        default=20.0,
        gt=0.0,
        description=(
            "Default replay speed multiplier applied to real inter-event "
            "gaps when streaming a dataset (e.g. 20.0 == 20x real time)."
        ),
    )
    replay_default_dataset_day: str = Field(
        default="friday-morning",
        description=(
            "Default capture day replayed as the landing stream (P5-8). "
            "Monday is 0.0% attack traffic (warmup-only, see "
            "warmup_dataset_day) — a live demo with zero real anomalies "
            "undercuts Invariant E. Friday-afternoon files are 55-57% "
            "attack traffic, unrealistically hostile for an operations "
            "console. friday-morning carries 1.03% real Bot/C2 traffic "
            "against otherwise-benign background — a realistic "
            "operational mix — so it is the authoritative default here; "
            "`ReplayFlowReader.iter_flows()` defers to this setting "
            "rather than holding its own copy (see docs/PHASE5_RECON.md "
            "section 0.5)."
        ),
    )
    warmup_dataset_day: str = Field(
        default="monday",
        description=(
            "Benign baseline day used to fit `StreamingScorer` "
            "(Ticket #5). Monday is 0.0% attack traffic with genuine "
            "second-resolution capture timestamps (see "
            "docs/PHASE5_RECON.md section 0.5) — a clean baseline for "
            "warmup fitting. Explicitly NOT the landing stream; see "
            "replay_default_dataset_day for that."
        ),
    )

    # ---- Model artifact ------------------------------------------------
    model_artifact_path: Path = Field(
        default=Path("artifacts/streaming_scorer.joblib"),
        description=(
            "Filesystem path to the persisted, joblib-serialized "
            "StreamingScorer (fit-once warmup artifact, Ticket #5). "
            "Lives under the gitignored artifacts/ directory."
        ),
    )

    # ---- StreamingScorer (Ticket #5) ------------------------------------
    streaming_contamination: float = Field(
        default=0.005,
        gt=0.0,
        lt=0.5,
        description=(
            "IsolationForest contamination for StreamingScorer's warmup "
            "fit. Deliberately NOT SETTINGS.ml.isolation_forest_contamination "
            "(0.08, used by the frozen Phase 3 batch benchmark) — the "
            "warmup slice is 100% benign by construction (see "
            "warmup_dataset_day), so contamination here is a stated "
            "FALSE-POSITIVE budget, not an anomaly-rate estimate. Measured "
            "on friday-morning against the full-Monday warmup model "
            "(docs/PHASE5_TICKET5_PLAN.md Q5): 0.08 flags 23.1 flows/sec "
            "of a 20x demo (a wall of red); 0.005 flags 1.73/sec (a "
            "visible, steady trickle that doesn't drown the tripwire's "
            "confidence-0.99 alert). Bounded strictly > 0: "
            "ml_engine.train_isolation_forest does "
            "`contamination or SETTINGS...`, which silently treats 0.0 as "
            "\"use the 0.08 default\" (the falsy-`or` trap, verified) — "
            "this field must never be passed as 0.0/0/None expecting "
            "'no flagging'."
        ),
    )
    streaming_n_estimators: int | None = Field(
        default=None,
        ge=10,
        le=1000,
        description=(
            "IsolationForest tree count for StreamingScorer's warmup fit. "
            "None (default) defers to SETTINGS.ml.isolation_forest_n_estimators "
            "(100) via ml_engine.train_isolation_forest's own optional-"
            "override fallback, so the tree count stays a single source of "
            "truth unless a deployment explicitly wants to diverge from "
            "the Phase 3 benchmark's setting."
        ),
    )
    warmup_row_limit: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional cap on how many (chronologically-first) warmup rows "
            "StreamingScorer.fit_from_warmup() reads. None (default) uses "
            "the full day (~529,918 Monday rows) — measured cost 4.72s "
            "end-to-end (docs/PHASE5_TICKET5_PLAN.md Q1 Measurement B), "
            "and subsampling was measured to corrupt explain()'s baseline "
            "sigma by up to 3.1x (Measurement C) for a saving of at most "
            "14% of an already-cheap build step. Exists for tests/CI that "
            "want a smaller, faster fixture, not for production use."
        ),
    )
    warmup_min_rows: int = Field(
        default=1000,
        ge=1,
        description=(
            "Hard floor on warmup row count. StreamingScorer.fit_from_warmup() "
            "raises below this. Guards against a degenerate zero-variance "
            "baseline (docs/PHASE5_TICKET5_PLAN.md Q3): measured head(1) "
            "yields 3 zero-variance feature columns, head(2) yields 2; by "
            "head(1000) all three columns already have non-zero variance "
            "of the right order of magnitude relative to the full day."
        ),
    )

    # ---- Replay engine (Ticket #6) ---------------------------------------
    replay_tick_interval_sec: float = Field(
        default=0.1,
        gt=0.0,
        le=5.0,
        description=(
            "Wall-clock interval between scheduling ticks in "
            "backend.replay_engine.ReplayEngine. Each tick emits every "
            "flow whose interpolated virtual time has come due as ONE "
            "micro-batch, never per-event (docs/PHASE5_STATE.md P5-10: "
            "measured per-event scoring+insert cost is 2.4x over budget "
            "at friday-morning's densest bucket; batched cost is ~40x "
            "under budget). 100ms balances a smooth live feed against "
            "per-tick overhead at the 20x default replay_speed."
        ),
    )
    replay_lag_warning_threshold_sec: float = Field(
        default=2.0,
        gt=0.0,
        le=60.0,
        description=(
            "If the replay engine's consumer falls this many virtual "
            "seconds behind the scheduled stream position, a warning is "
            "logged once per lag episode (not per tick — a slow consumer "
            "must not flood the log). The engine never drops events to "
            "catch up; lag is measured and exposed via "
            "ReplayEngine.status().lag_seconds, never silently absorbed."
        ),
    )
    replay_injection_queue_max: int = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
        description=(
            "Maximum flows ReplayEngine.inject() may hold queued for "
            "emission on the engine's next tick, ahead of the scheduled "
            "replay stream. inject() raises ReplayEngineError rather than "
            "silently dropping flows when this cap would be exceeded."
        ),
    )
    replay_thread_join_timeout_sec: float = Field(
        default=5.0,
        gt=0.0,
        le=120.0,
        description=(
            "Bound on how long ReplayEngine.stop() waits for the "
            "background replay thread to join before returning. A slow "
            "in-flight consumer call can delay shutdown past this bound; "
            "stop() is best-effort bounded, it does not block "
            "indefinitely on a wedged consumer."
        ),
    )
    replay_max_batch_size: int = Field(
        default=500,
        ge=1,
        le=100_000,
        description=(
            "Upper bound on how many flows ReplayEngine emits in a single "
            "scheduled batch. Without a cap, batch size scales linearly "
            "with the operator-controllable speed multiplier — measured "
            "on friday-morning's densest bucket: speed=20x -> max_batch=87 "
            "(~0.03MB est. WS frame), speed=200x -> max_batch=860 "
            "(~0.29MB), speed=2000x -> max_batch=3,955 (~1.32MB). An "
            "operator sliding speed up would otherwise hand Ticket #7 a "
            "multi-thousand-row bulk insert and Ticket #9 a multi-"
            "megabyte WebSocket frame in one shot. When more flows are "
            "due in a tick than this cap, the engine emits exactly the "
            "cap and carries the remainder forward to the next tick "
            "(never drops flows); the resulting schedule slippage is "
            "reported honestly via status().lag_seconds rather than "
            "hidden. 500 comfortably covers the demo path (20x, max 87) "
            "with headroom before any WS-frame-size concern."
        ),
    )

    # ---- Injection (Ticket #13) -------------------------------------------
    inject_max_flows: int = Field(
        default=500,
        ge=1,
        le=10_000,
        description=(
            "Hard cap on `count` for POST /api/inject. Injected flows are "
            "handed straight to `ReplayEngine.inject()`, which emits them "
            "as ONE micro-batch on the engine's very next tick — an "
            "operator-supplied `count` with no ceiling would hand the "
            "ingest pipeline (scoring + a bulk INSERT) and the WebSocket "
            "broadcaster an arbitrarily large burst in one shot, the same "
            "risk `replay_max_batch_size` already guards against for the "
            "scheduled stream. 500 matches that default and comfortably "
            "covers the demo scenarios (bot_c2 has 1,966 real matching "
            "flows to draw from; ddos and port_scan have well over "
            "100,000), so the cap is never the limiting factor for a "
            "realistic what-if burst."
        ),
    )

    # ---- Retention (Ticket #2) ------------------------------------------
    db_event_retention_max_rows: int = Field(
        default=500_000,
        ge=1000,
        description=(
            "Maximum number of rows retained in the events table. "
            "backend.retention.prune_events() deletes the oldest events "
            "(by `ts`, event time) beyond this count so a long-running demo "
            "doesn't accumulate unbounded rows. Wiring prune_events() to a "
            "periodic call is Ticket #7 — this setting and the function "
            "itself are provided in Ticket #2."
        ),
    )

    # ---- Ingest pipeline (Ticket #7) ------------------------------------
    cii_debounce_sec: float = Field(
        default=30.0,
        ge=0.0,
        le=3600.0,
        description=(
            "Minimum wall-clock seconds between two CII recomputations for "
            "the SAME origin asset. compute_cascading_impact_full() runs "
            "SETTINGS.cii.mc_iterations Monte Carlo BFS passes per call — "
            "far too expensive to run per anomalous event when a single "
            "friday-morning replay produces ~800 volumetric anomalies "
            "(docs/DETECTION_STUDY.md). Within the window the cached "
            "CIIResult is reused and re-linked rather than recomputed, so "
            "an alert always carries a blast radius; only the recomputation "
            "is skipped, never the linkage."
        ),
    )

    cii_cache_max_entries: int = Field(
        default=256,
        ge=1,
        le=100_000,
        description=(
            "Upper bound on distinct origin assets held in the ingest CII "
            "debounce cache. Bounded because AssetRegistry auto-registers "
            "one Unresolved_<ip> asset per unique unresolved IP (risk T5) — "
            "real CIC-IDS2017 has thousands, so an unbounded cache is an "
            "unbounded memory leak over a long replay. Evicted "
            "least-recently-used."
        ),
    )

    alert_on_volumetric: bool = Field(
        default=False,
        description=(
            "Whether a volumetric-only anomaly (IsolationForest fired, "
            "tripwire did not) may raise an operator alert. Default False "
            "on measured evidence, not caution: docs/DETECTION_STUDY.md "
            "records 5 true positives against 811 false positives on real "
            "replayed friday-morning traffic (precision ~0.02). Alerting on "
            "that channel fills the alerts panel with ~800 junk rows per "
            "replay day and buries the tripwire alert that the demo's "
            "headline moment depends on. The volumetric channel is still "
            "fully scored, persisted to event_scores, and broadcast — it is "
            "visible in the live feed; it just does not page an operator. "
            "Set True only with alert_volumetric_min_calibrated_score tuned."
        ),
    )

    alert_volumetric_min_calibrated_score: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description=(
            "Calibrated-score floor a volumetric-only anomaly must clear to "
            "raise an alert when alert_on_volumetric is True. "
            "ml_engine.compute_anomaly_scores emits calibrated_score in "
            "[0, 1] (sigmoid of raw_score), so this is a probability-scale "
            "threshold. Ignored entirely when alert_on_volumetric is False."
        ),
    )

    alert_asset_debounce_sec: float = Field(
        default=60.0,
        ge=0.0,
        le=3600.0,
        description=(
            "Minimum wall-clock seconds between two alerts naming the same "
            "asset. Prevents one sustained anomalous flow burst from "
            "producing hundreds of near-identical rows in the alerts panel. "
            "Applies to every alert channel including tripwire: a "
            "honeytoken touch repeated 400 times is one incident, not 400 "
            "alerts. Set 0.0 to disable de-duplication."
        ),
    )

    # ---- API (Ticket #16): risk index -------------------------------------
    # GET /api/stats' `risk_index` is a DEFINED, DERIVED quantity, never an
    # invented number (docs/PHASE5_TICKET16_PLAN.md section 3, decision
    # D16-1): sum of (severity_weight x asset_criticality) over
    # UNACKNOWLEDGED alerts, normalised against risk_index_full_scale. It
    # is deliberately NOT built on CII -- measured across all 50 assets in
    # config.SMART_CITY_ASSETS this session, CII is currently near-binary
    # (28 report exactly 0.0, 18 exactly 1.0, only 4 in between), and
    # feeding that degeneracy into the first number an operator reads
    # would propagate it into the headline figure rather than fix it.
    risk_severity_weight_critical: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description=(
            "Contribution weight for an unacknowledged 'critical'-severity "
            "alert (backend.ingest.SEVERITY_CRITICAL) in the GET /api/stats "
            "risk index. The highest severity weight: today only a "
            "tripwire hit (an unambiguous honeytoken touch, which cannot "
            "be a false positive by construction -- docs/DETECTION_STUDY.md "
            "section 5) is raised at this severity."
        ),
    )
    risk_severity_weight_warning: float = Field(
        default=0.35,
        ge=0.0,
        le=10.0,
        description=(
            "Contribution weight for an unacknowledged 'warning'-severity "
            "alert (backend.ingest.SEVERITY_WARNING -- a volumetric "
            "anomaly that cleared alert_volumetric_min_calibrated_score) "
            "in the risk index. Well below the critical weight because "
            "docs/DETECTION_STUDY.md measured this channel at ~0.02 "
            "precision on real replayed traffic: it should still move the "
            "index, but far less per alert than an unambiguous tripwire "
            "hit."
        ),
    )
    risk_severity_weight_default: float = Field(
        default=0.1,
        ge=0.0,
        le=10.0,
        description=(
            "Fallback contribution weight for any alert severity string "
            "other than 'critical' or 'warning'. backend.ingest raises "
            "only those two severities today, but Alert.severity is a "
            "plain unconstrained `str` column (backend/models.py) -- this "
            "exists so a future third tier (e.g. 'normal', per "
            "docs/PHASE5_TICKET16_PLAN.md section 3's illustrative "
            "'critical > warning > normal' ordering) contributes "
            "proportionally to the risk index instead of being silently "
            "dropped or raising a KeyError."
        ),
    )
    risk_index_full_scale: float = Field(
        default=5.0,
        gt=0.0,
        le=1000.0,
        description=(
            "Denominator for GET /api/stats' risk_index: clamp(0, 100) of "
            "100 * sum(severity_weight * asset_criticality over "
            "unacknowledged alerts) / risk_index_full_scale. THIS IS A "
            "PRESENTATION SCALE, NOT A CALIBRATED PROBABILITY -- there is "
            "no ground truth for what '100% risk' means in this system, "
            "only a chosen denominator that makes the number legible. "
            "Chosen so a single critical-severity alert on the single "
            "highest-criticality asset in the graph (criticality 1.0) "
            "reads as a clearly-visible ~20/100, leaving headroom for "
            "several concurrent unacknowledged alerts before the index "
            "saturates at 100."
        ),
    )

    ingest_retention_check_every_n_batches: int = Field(
        default=200,
        ge=1,
        le=1_000_000,
        description=(
            "How often (in ingested batches) backend.retention.prune_events "
            "is called to enforce db_event_retention_max_rows. Ticket #2 "
            "provided prune_events() and explicitly deferred wiring it to a "
            "periodic call to this ticket. Not every batch: the prune issues "
            "a COUNT plus a DELETE over the events table, which is wasted "
            "work when a batch adds at most replay_max_batch_size (500) rows "
            "against a 500,000-row cap."
        ),
    )

    # ---- WebSocket transport (Ticket #9) ----------------------------------
    ws_client_queue_max: int = Field(
        default=1000,
        ge=1,
        le=1_000_000,
        description=(
            "Per-client bounded asyncio.Queue size for WS /ws/stream "
            "(decision D9-2). Ticket #7 publishes one envelope PER EVENT, "
            "and P5-12 measured speed=2000x producing 500-flow batches, "
            "so a slow browser tab must never become the replay engine's "
            "rate limiter: on overflow the OLDEST queued envelope for that "
            "client is dropped (never the newest, and never blocking the "
            "publisher) and a per-connection dropped-counter is "
            "incremented. 1000 gives a live view several seconds of "
            "buffer at the 20x demo default before anything is dropped."
        ),
    )
    ws_send_timeout_sec: float = Field(
        default=5.0,
        gt=0.0,
        le=120.0,
        description=(
            "Bound on how long a WS /ws/stream per-client writer task may "
            "block on WebSocket.send_json() for one envelope before that "
            "send is treated as failed and the connection is torn down. "
            "Without a bound, a stuck TCP write (e.g. a client whose OS "
            "receive buffer is full and never drained) would pin the "
            "writer task -- and therefore that client's queue -- forever; "
            "other clients are unaffected either way (each has its own "
            "writer task), but an unbounded wait here would also delay "
            "that client's own disconnect from ever being noticed."
        ),
    )

    # ---- API (Ticket #8) --------------------------------------------------
    api_cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://127.0.0.1:3000"],
        description=(
            "Allowed CORS origins for the FastAPI app (Ticket #8, decision "
            "D8-4). The Next.js console (Ticket #3) runs on localhost:3000; "
            "this API runs on api_port (8000 by default) — different "
            "origin, so every browser call fails without CORS. Deliberately "
            "NOT ['*']: these routes include unauthenticated state-changing "
            "controls (POST /api/replay/start|stop|speed), and a wildcard "
            "origin alongside a wide api_host bind would hand the LAN "
            "unauthenticated control of the demo — exactly the risk "
            "api_host's own docstring already guards against."
        ),
    )
    api_events_default_limit: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description=(
            "Page size for GET /api/events when the caller omits `limit`. "
            "Must be <= api_events_max_limit (enforced by "
            "_check_api_default_limits_within_cap below)."
        ),
    )
    api_events_max_limit: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description=(
            "Hard cap on GET /api/events' `limit` query parameter. A "
            "request above this is rejected with 422 via the route's "
            "Pydantic Query bounds, never silently clamped — a silent "
            "clamp would lie to the caller about how many rows it is "
            "actually getting. Without a cap, `limit` is unbounded against "
            "a table db_event_retention_max_rows (500,000 by default) "
            "deliberately lets grow large, so one request could otherwise "
            "pull the whole retention window in one response. Also reused "
            "as the hard cap for GET /api/alerts' `limit` — the plan "
            "(docs/PHASE5_TICKET8_PLAN.md section 7) defines only one "
            "'max' field, and the same unbounded-pull risk applies equally "
            "to the alerts table."
        ),
    )
    api_alerts_default_limit: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description=(
            "Page size for GET /api/alerts when the caller omits `limit`. "
            "Bounded by api_events_max_limit — see that field's docstring "
            "for why alerts has no separate max-limit field of its own."
        ),
    )

    @model_validator(mode="after")
    def _check_api_default_limits_within_cap(self) -> "BackendSettings":
        """Both default-limit fields must not exceed api_events_max_limit
        (the shared hard cap — see its docstring). A misconfigured .env
        that set a default above the cap would otherwise silently serve
        fewer rows than the operator asked for on every un-paginated
        request; failing fast at settings-construction time surfaces that
        immediately instead of as a confusing runtime discrepancy."""
        if self.api_events_default_limit > self.api_events_max_limit:
            raise ValueError(
                "api_events_default_limit "
                f"({self.api_events_default_limit}) must be <= "
                f"api_events_max_limit ({self.api_events_max_limit})"
            )
        if self.api_alerts_default_limit > self.api_events_max_limit:
            raise ValueError(
                "api_alerts_default_limit "
                f"({self.api_alerts_default_limit}) must be <= "
                f"api_events_max_limit ({self.api_events_max_limit}) — "
                "alerts reuses the events hard cap, see its docstring."
            )
        return self

    # Singleton access pattern, matching src/settings.py's AEGISSettings.
    _instance: ClassVar["BackendSettings | None"] = None

    @property
    def database_url(self) -> str:
        """SQLAlchemy connection URL, using the psycopg v3 driver.

        Returns `db_url_override` verbatim if set (the AEGIS_DATABASE_URL
        escape hatch — supplied pre-formed by the operator, so it is never
        re-encoded). Otherwise composes a `postgresql+psycopg://` URL from
        the individual db_* component fields, percent-encoding `db_user`
        and `db_password` (via `urllib.parse.quote_plus`) so credentials
        containing URL-special characters (`@ : / # ?`) don't break the
        connection URL's own delimiter parsing.
        """
        if self.db_url_override:
            return self.db_url_override
        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def model_artifact_path_resolved(self) -> Path:
        """`model_artifact_path`, guaranteed absolute and CWD-independent.

        A relative `model_artifact_path` is resolved against the repo root
        (`_REPO_ROOT`, derived from `__file__`, not from the process's
        current working directory) so that launching uvicorn from any
        directory still finds the same artifact. An absolute path supplied
        by the operator is returned unchanged. Callers should use this
        property rather than `model_artifact_path` directly to avoid
        accidentally depending on CWD.
        """
        if self.model_artifact_path.is_absolute():
            return self.model_artifact_path
        return _REPO_ROOT / self.model_artifact_path


# Module-level singleton — import this everywhere.
BACKEND_SETTINGS = BackendSettings()
