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
from typing import ClassVar, Optional
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

    # ---- Security (Phase B improvement pass) ------------------------------
    api_token: Optional[str] = Field(
        default=None,
        description=(
            "Bearer token required on every state-changing route (POST "
            "/api/replay/start|stop|speed, POST /api/inject, POST "
            "/api/alerts/{id}/ack) when set — `None` (the default) leaves "
            "those routes exactly as unauthenticated as before, matching "
            "api_host's own default posture: safe out of the box for a "
            "loopback-bound local demo, opt-in to tighten. Read this "
            "honestly: a token shipped to the browser via "
            "NEXT_PUBLIC_API_TOKEN is visible to anyone who reads the "
            "page's own JS bundle, so this is NOT resistant to a targeted "
            "attacker with devtools open on the page itself — it stops an "
            "unrelated web page, an opportunistic scanner, or a stray "
            "curl from a LAN neighbour from finding an open, undocumented "
            "control surface and using it, which is the actual threat "
            "model for `api_host=0.0.0.0` (venue wifi, container "
            "networking). A startup warning fires (see `main.py`'s "
            "lifespan) if `api_host` is opened to the LAN while this is "
            "still unset."
        ),
    )
    rate_limit_max_requests: int = Field(
        default=30,
        ge=1,
        description=(
            "Max requests a single client IP may make to a state-changing "
            "route within rate_limit_window_sec, before further ones get "
            "429. Applies to the same route set api_token protects. "
            "In-memory, per-process — resets on restart, does not survive "
            "multiple backend instances (this project runs exactly one, "
            "see PLAN_MASTER.md's explicit multi-instance/multi-tenancy "
            "deferral)."
        ),
    )
    rate_limit_window_sec: float = Field(
        default=60.0,
        gt=0.0,
        description="Sliding window (seconds) rate_limit_max_requests is measured over.",
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

    # ---- SupervisedFlowScorer (Phase B improvement pass) ------------------
    supervised_model_artifact_path: Path = Field(
        default=Path("artifacts/supervised_flow_scorer.joblib"),
        description=(
            "Filesystem path to the persisted, joblib-serialized "
            "SupervisedFlowScorer (fit-once build artifact, mirroring "
            "model_artifact_path/StreamingScorer exactly). Lives under the "
            "gitignored artifacts/ directory. Unlike model_artifact_path, "
            "a MISSING artifact here is not fatal to the API — the "
            "known-threat channel is an additive third detector; its "
            "absence degrades to 'two live channels' (Isolation Forest + "
            "tripwire), never a 503 on replay-control routes. See "
            "backend/runtime.py's build_runtime()."
        ),
    )
    supervised_train_split_fraction: float = Field(
        default=0.5,
        gt=0.0,
        lt=1.0,
        description=(
            "Fraction of BACKEND_SETTINGS.replay_default_dataset_day used "
            "to train SupervisedFlowScorer at build time — the FIRST "
            "split_fraction of the day, chronologically. Deliberately the "
            "exact same value and methodology as "
            "backend.supervised_detector.temporal_split_evaluate()'s own "
            "default (0.5, 'train on the earlier half, test on the later "
            "half of one day') — the live deployment reproduces the "
            "already-published, already-defended docs/DETECTION_STUDY.md "
            "Test 1 methodology rather than inventing a new one. Read "
            "SupervisedFlowScorer's class docstring before assuming this "
            "means the live channel is unbiased on the demo day: because "
            "replay always restarts a day from position 0, it has "
            "genuinely seen the labels for roughly the first half of any "
            "live friday-morning replay."
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
    db_event_retention_max_age_days: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional age-based retention bound, applied alongside "
            "db_event_retention_max_rows rather than instead of it — "
            "prune_events() deletes any event caught by EITHER bound (the "
            "oldest rows beyond the row cap, and every row older than this "
            "many days). None (the default) disables the age bound "
            "entirely, matching this setting's absence before Phase C — "
            "row-count retention alone was sufficient at demo scale, but "
            "the first question a real-deployment review asks is 'how long "
            "is data kept', which a pure row cap can't answer on its own."
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

    # ---- Hybrid IDS: fusion layer -----------------------------------------
    hybrid_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the Hybrid IDS layer (backend/detection/). "
            "When True the signature and beaconing detectors run, their "
            "verdicts are fused, and the fused decision is persisted as a "
            "'hybrid' event_scores row and included in the live WebSocket "
            "envelope. When False the pipeline behaves exactly as it did "
            "before the hybrid layer existed — the switch is what makes "
            "that claim testable rather than asserted."
        ),
    )
    hybrid_gates_alerts: bool = Field(
        default=False,
        description=(
            "Whether the fused decision may CREATE alerts the existing "
            "policy would not have created. Default False deliberately: "
            "P5-15's alert policy (tripwire always alerts, volumetric-only "
            "is suppressed at ~0.02 precision) and every alert/risk figure "
            "already published are derived from that policy, so the hybrid "
            "layer ships observable-but-not-authoritative first. Turning "
            "this on is a policy change that must be re-measured, not a "
            "tuning knob. A CONFIRMED signal (honeytoken) alerts either "
            "way — that path never depended on this flag."
        ),
    )
    hybrid_band_suspicious: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description=(
            "Fused threat_score at or above which a decision is banded "
            "SUSPICIOUS rather than BENIGN (ThreatBand, "
            "backend/detection/contracts.py)."
        ),
    )
    hybrid_band_likely: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="Fused threat_score at or above which a decision is banded LIKELY.",
    )
    hybrid_band_confirmed: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Fused threat_score at or above which a decision is banded "
            "CONFIRMED. A Certainty.CONFIRMED verdict (honeytoken) reaches "
            "this band by precedence regardless of the numeric score — see "
            "HybridFusionEngine's precedence rule."
        ),
    )

    # ---- Hybrid IDS: per-detector reliability weights ---------------------
    # Every default below is a MEASURED figure from docs/DETECTION_STUDY.md,
    # not a tuned guess. Fusion multiplies each detector's calibrated score
    # by its weight, so these are the single place where "how much do we
    # trust this channel" is stated, and they are stated honestly — the
    # volumetric channel's 0.02 is its real precision, and keeping it that
    # low is what stops ~800 junk signals per replay day from moving the
    # fused score.
    hybrid_weight_volumetric: float = Field(
        default=0.02,
        ge=0.0,
        le=1.0,
        description=(
            "Reliability weight for the unsupervised volumetric channel "
            "(StreamingScorer). 0.02 IS its measured precision on real "
            "friday-morning traffic (5 TP / 811 FP). Deliberately not "
            "rounded up: a detector this imprecise must not be able to "
            "raise the fused score on its own."
        ),
    )
    hybrid_weight_supervised: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        description=(
            "Reliability weight for the supervised known-threat channel "
            "(SupervisedFlowScorer). Its measured in-distribution "
            "precision is 0.998 (temporal split), but the SAME study "
            "measured precision 0.000 on a novel attack family, so the "
            "weight is discounted below the in-distribution figure rather "
            "than taking the flattering number at face value."
        ),
    )
    hybrid_weight_tripwire: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Reliability weight for the honeytoken tripwire. 1.0 because a "
            "credential with zero legitimate use cannot produce a false "
            "positive. Note the weight is not what makes the tripwire "
            "decisive — Certainty.CONFIRMED precedence is."
        ),
    )
    hybrid_weight_signature: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Reliability weight for the rule/signature engine. High "
            "because a matched rule is an exact statement about observable "
            "flow metadata, not an inference — but below 1.0 because the "
            "rules match on metadata only (CIC-IDS2017 carries no "
            "payloads), so a benign flow can legitimately look like a "
            "rule's target."
        ),
    )
    hybrid_weight_beaconing: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description=(
            "Reliability weight for the temporal/beaconing detector. "
            "Deliberately mid-range and explicitly UNMEASURED on this "
            "corpus at time of writing — unlike the other four weights "
            "there is no precision figure behind it yet. Treat it as a "
            "placeholder to be replaced by a measurement, and do not cite "
            "it as evidence of the channel's quality."
        ),
    )
    hybrid_weight_tgnn: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description=(
            "Reliability weight for the graph-structural (T-GNN) detector. "
            "MEASURED, not the earlier 0.50 placeholder: precision = "
            "TP/(TP+FP) = 560/(560+3166) = 0.15 on the same CIC-IDS2017 "
            "friday-morning replay used to tune tgnn_window_sec/"
            "tgnn_min_edges_to_score/tgnn_contamination above (Bot fired "
            "560 of 1966 = 28.48% recall; BENIGN fired 3166 of 189067 = "
            "1.67%). Same methodology as hybrid_weight_volumetric's 0.02 "
            "(docs/DETECTION_STUDY.md). Caveat this shares with every "
            "other weight here: one dataset, one day, one attack family "
            "(Bot/Ares C2) — not cross-validated against a second corpus "
            "or a second attack type, so treat 0.15 as a real first "
            "measurement, not a ceiling on the channel's eventual quality."
        ),
    )

    # ---- Hybrid IDS: beaconing (temporal) detector ------------------------
    beaconing_enabled: bool = Field(
        default=True,
        description=(
            "Whether the temporal/periodicity detector runs. It addresses "
            "the volumetric channel's documented structural blind spot: "
            "Bot C2 beacons are SMALLER than benign traffic (median 6 "
            "bytes vs 70), so an outlier detector over volume looks in the "
            "wrong direction, while the beacon's regular inter-arrival "
            "rhythm is a signal no per-flow volumetric feature can carry."
        ),
    )
    beaconing_min_samples: int = Field(
        default=5,
        ge=3,
        le=1000,
        description=(
            "Minimum flows observed for one (src_ip, dst_ip) pair before "
            "the beaconing detector will render any verdict on it. Below "
            "this the interval sample is too small for a coefficient of "
            "variation to mean anything, and the detector abstains "
            "(fired=False, calibrated_score=0.0) rather than guessing. "
            "Minimum 3 because n intervals needs n+1 observations."
        ),
    )
    beaconing_history_per_pair: int = Field(
        default=32,
        ge=4,
        le=4096,
        description=(
            "Timestamps retained per tracked pair (ring buffer). Bounds "
            "per-pair memory and keeps the CV window recent — a beacon that "
            "started an hour ago should be judged on its current rhythm."
        ),
    )
    beaconing_max_tracked_pairs: int = Field(
        default=20_000,
        ge=100,
        le=2_000_000,
        description=(
            "LRU cap on tracked (src_ip, dst_ip) pairs. Unbounded state is "
            "a real risk here, not a theoretical one: risk T5 records that "
            "one auto-registered node appears per unique unresolved IP, and "
            "a replay day carries hundreds of distinct /24s. Mirrors the "
            "bounded-OrderedDict approach already used for cii_cache and "
            "the per-asset alert debounce map."
        ),
    )
    beaconing_max_cv: float = Field(
        default=0.25,
        ge=0.0,
        le=10.0,
        description=(
            "Coefficient of variation (stddev/mean of inter-arrival "
            "intervals) at or below which a pair's traffic counts as "
            "regular enough to be a beacon. Low CV means metronomic "
            "timing; human-driven traffic is bursty and scores far higher. "
            "0.25 tolerates the jitter real C2 frameworks add on purpose."
        ),
    )
    beaconing_min_interval_sec: float = Field(
        default=0.5,
        gt=0.0,
        description=(
            "Intervals shorter than this are excluded from the CV "
            "computation. Without the floor, a burst of flows sharing one "
            "minute-granularity timestamp (friday-morning peaks at 4,017 "
            "such rows, see P5-11) yields near-zero intervals with "
            "near-zero variance — a perfect fake beacon manufactured "
            "entirely by timestamp resolution."
        ),
    )
    beaconing_max_interval_sec: float = Field(
        default=3600.0,
        gt=0.0,
        description=(
            "Intervals longer than this are excluded from the CV "
            "computation — a gap that large means the channel went away and "
            "came back, not that it beaconed slowly."
        ),
    )

    # ---- Hybrid IDS: T-GNN (graph-structural) detector --------------------
    tgnn_enabled: bool = Field(
        default=True,
        description=(
            "Whether the graph-structural detector runs. It addresses the "
            "blind spot shared by every other channel: an attacker who is "
            "volumetrically quiet, temporally regular, and rule-compliant "
            "but talks to unusual peers or concentrates traffic in an "
            "unusual way is invisible to size/timing/rule-based detectors "
            "but visible in the communication graph's topology."
        ),
    )
    tgnn_window_sec: float = Field(
        default=60.0,
        gt=0.0,
        description=(
            "Sliding window (seconds) for graph edge retention. Edges not "
            "refreshed within this window are pruned, so the graph reflects "
            "recent structural state rather than the entire session's "
            "cumulative history. "
            "MEASURED against the 2026-09-04 self-temporal-drift pivot's "
            "offline replay (CIC-IDS2017 friday-morning): at the prior "
            "default of 300s, a window this wide over real bursty traffic "
            "captures enough incidental peer churn that neighbor_drift's "
            "median sits above 0.8 for BENIGN flows too, drowning the "
            "signal (benign fired 9.96%, Bot only 3.15%). 60s narrows the "
            "window to traffic that is actually concurrent, cutting "
            "incidental churn enough to separate the two (benign 1.67%, "
            "Bot 28.48%, together with tgnn_min_edges_to_score=4 and "
            "tgnn_contamination=0.1 below)."
        ),
    )
    tgnn_max_nodes: int = Field(
        default=500,
        ge=10,
        le=100_000,
        description=(
            "LRU cap on tracked graph nodes (IPs). Mirrors "
            "beaconing_max_tracked_pairs — unbounded per-node state is a "
            "real leak risk over a long-running stream given how many "
            "distinct IPs a replay day can carry."
        ),
    )
    tgnn_baseline_batches: int = Field(
        default=10,
        ge=1,
        le=10_000,
        description=(
            "Number of ingest batches assumed benign before the detector "
            "fits its IsolationForest baseline and switches from abstaining "
            "to scoring. Mirrors StreamingScorer's fit-on-benign-first "
            "convention. If the demo starts with an injection immediately, "
            "the baseline will be poisoned — same limitation the volumetric "
            "channel already has. "
            "MEASURED, not guessed: a full friday-morning replay at 40x "
            "produced only 49 ingest batches end to end, so the original "
            "value of 50 meant the baseline was NEVER fitted and the "
            "channel abstained on all 1,080 ingested flows — a silently "
            "dead detector. 10 fits early enough to leave the large "
            "majority of a replay in scoring mode."
        ),
    )
    tgnn_min_baseline_nodes: int = Field(
        default=8,
        ge=2,
        le=10_000,
        description=(
            "Minimum number of accumulated training ROWS (one scorable "
            "node's feature vector from one batch — see "
            "tgnn_max_training_rows) required before the baseline will be "
            "fitted at all. The forest is deliberately fitted on the same "
            "SCORABLE population it later scores (out-degree past "
            "tgnn_min_edges_to_score) — see `_fit_baseline`'s docstring "
            "for the measurement showing why fitting on the whole graph "
            "instead saturates the channel at a 100% firing rate — and that "
            "population is small, so this floor stops an IsolationForest "
            "being fitted on two or three points, where its notion of an "
            "outlier is meaningless. Below the floor the detector keeps "
            "abstaining, which is the honest answer while it has nothing to "
            "compare against."
        ),
    )
    tgnn_refit_every_batches: int = Field(
        default=100,
        ge=1,
        le=100_000,
        description=(
            "Refit the structural baseline every N batches once it has "
            "first been fitted, so the reference distribution tracks the "
            "traffic's CURRENT normal instead of staying frozen on the "
            "first window ever seen. "
            "MEASURED, not guessed: with a single frozen baseline fitted at "
            "batch 10, the graph is still tiny and unrepresentative, and "
            "every later node falls outside its range — the percentile "
            "score came out p10=0.947 / p50=0.995 over 33,372 scored real "
            "flows, i.e. essentially everything read as an outlier and the "
            "channel fired on 100% of what it scored. Periodic refitting is "
            "what makes the calibration mean anything on a graph that grows "
            "over a session. "
            "Tradeoff, stated plainly: structural change that persists "
            "across a refit is absorbed into the new normal, so this "
            "detects a CHANGE in topology rather than a standing "
            "misconfiguration — the same class of limitation as the "
            "benign-baseline assumption above."
        ),
    )
    tgnn_min_edges_to_score: int = Field(
        default=4,
        ge=1,
        le=10_000,
        description=(
            "Minimum outgoing edges a node must have before it is "
            "considered scorable. Below this, the sample is too small for "
            "the temporal-drift features (unseen-peer ratio, degree "
            "expansion, neighbor drift, entropy delta) to mean anything, "
            "and the detector abstains rather than guessing. "
            "MEASURED against the 2026-09-04 self-temporal-drift pivot's "
            "offline replay (CIC-IDS2017 friday-morning, paired with "
            "tgnn_window_sec=60 and tgnn_contamination=0.1): 3 left in "
            "enough low-context, one-off nodes that BENIGN firing sat at "
            "9.96% against Bot's 3.15% — inverted. 4 filters out that "
            "low-context noise; BENIGN firing fell to 1.67% and Bot rose "
            "to 28.48%."
        ),
    )
    tgnn_max_training_rows: int = Field(
        default=5_000,
        ge=50,
        le=1_000_000,
        description=(
            "Cap on the rolling buffer of per-batch, per-scorable-node "
            "temporal-drift feature rows the IsolationForest is fitted on "
            "(a `collections.deque(maxlen=...)`, oldest rows drop first). "
            "Rows are collected every batch — including before the "
            "baseline is first fitted — because a SINGLE snapshot compared "
            "against itself gives every delta feature exactly zero "
            "variance (unseen-peer ratio, degree expansion, drift and "
            "entropy delta are all trivially 0 when 'current' and "
            "'baseline' are read at the same instant), and an "
            "IsolationTree cannot split on a zero-variance column. "
            "Accumulating rows across many batches captures REAL temporal "
            "spread instead — established nodes contribute near-zero "
            "drift rows while newly-appeared nodes contribute high-drift "
            "ones — which is what makes 'unusual drift' answerable at "
            "fit time. The rolling window also means a periodic refit "
            "trains on recent behaviour rather than the session's entire "
            "history. At 4 float32 features per row the cap costs "
            "kilobytes, not a scaling concern."
        ),
    )
    tgnn_max_history_peers_per_node: int = Field(
        default=2_000,
        ge=10,
        le=1_000_000,
        description=(
            "LRU cap (most-recently-touched peer wins) on the number of "
            "distinct out-peers HISTORY remembers per node. "
            "`tgnn_max_nodes` bounds the number of TRACKED NODES, but "
            "says nothing about how large any one node's own peer set "
            "can grow — a long-lived hub (a gateway, a proxy) that "
            "survives the whole session while continuously reaching new "
            "destinations would otherwise accumulate an unbounded "
            "per-node peer history over a multi-day run even though the "
            "node count itself stays capped. "
            "MEASURED, not guessed, and tightly coupled to the other "
            "tgnn_* defaults: over the full CIC-IDS2017 friday-morning "
            "replay (the same one tgnn_window_sec/tgnn_min_edges_to_score/"
            "tgnn_contamination/hybrid_weight_tgnn are tuned against), the "
            "busiest node accumulates 1,301 distinct historical peers. An "
            "initial value of 200 clipped that and 9 other genuinely busy "
            "nodes' baseline degree, which artificially inflates "
            "degree_expansion for perfectly ordinary hubs — the exact "
            "hub-inflation failure mode the 2026-09-04 pivot away from "
            "pooled centrality was meant to close, reopened through the "
            "back door: BENIGN firing rose from 1.67% to 9.65% at cap=200 "
            "on the identical replay. 2,000 sits comfortably above the "
            "observed maximum (headroom for a longer real deployment) "
            "while still being a real, finite ceiling rather than a "
            "nominal one — verified to reproduce the exact 1.67%/28.48% "
            "BENIGN/Bot figures the other three settings above cite."
        ),
    )
    tgnn_fire_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Calibrated score at or above which a T-GNN verdict fires. "
            "Applied to the linear map of IsolationForest's anomaly score "
            "onto [0, 1] — see tgnn.py's module docstring for the mapping."
        ),
    )
    tgnn_contamination: float = Field(
        default=0.1,
        gt=0.0,
        le=0.5,
        description=(
            "IsolationForest's contamination parameter — the assumed "
            "fraction of structurally anomalous rows in the training "
            "buffer used to set its internal decision threshold. "
            "MEASURED against the 2026-09-04 self-temporal-drift pivot's "
            "offline replay (paired with tgnn_window_sec=60 and "
            "tgnn_min_edges_to_score=4): 0.05 under-fired on Bot (19.79% "
            "vs BENIGN 0.46% — correctly ordered but short of the >25% "
            "target); 0.1 reached BENIGN 1.67% / Bot 28.48%. 0.15 pushed "
            "Bot to 30.11% but also BENIGN to 4.61%, most of the way back "
            "to the 5% ceiling — 0.1 was kept as the safer margin."
        ),
    )

    # ---- Hybrid IDS: signature/rule engine --------------------------------
    signature_enabled: bool = Field(
        default=True,
        description=(
            "Whether the rule/signature detector runs. Rules match on flow "
            "METADATA only (ports, protocol, byte/packet shape, known-bad "
            "addresses) because CIC-IDS2017 TrafficLabelling carries flow "
            "records, not payloads — so this is not a payload-inspecting "
            "IDS like Snort/Suricata, and its rules must not be described "
            "as signatures over packet contents."
        ),
    )
    signature_small_payload_bytes: int = Field(
        default=64,
        ge=0,
        description=(
            "Byte ceiling below which a flow to an external address counts "
            "as 'small payload' for the C2-shaped rule. Anchored to the "
            "measured Bot C2 median of 6 bytes (vs 70 for benign) with "
            "headroom, per docs/DETECTION_STUDY.md."
        ),
    )

    # ---- IPS: prevention policy layer --------------------------------------
    # Sits downstream of the Hybrid IDS layer above: `IPSPolicyEngine`
    # (backend/ips/policy.py) consumes a `FusedDecision` (already fused,
    # already weighted) plus asset criticality and CII median impact, and
    # decides among observe / alert / rate-limit / block / quarantine. See
    # that module's docstring for the full decision tree; the fields below
    # are its configurable inputs, following the exact optional-override /
    # BACKEND_SETTINGS-fallback convention the Hybrid IDS fields above use.
    ips_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the IPS (prevention) layer. When False, no "
            "IPS decision is ever computed, persisted, enforced, or "
            "broadcast — the pipeline behaves exactly as it did before "
            "this layer existed, mirroring hybrid_enabled's role for the "
            "Hybrid IDS layer. Off by default: unlike the advisory Hybrid "
            "IDS layer, this layer can actively act on a decision (even if "
            "only in simulation via ips_dry_run below), so it ships opt-in "
            "rather than on-but-non-authoritative."
        ),
    )
    ips_dry_run: bool = Field(
        default=True,
        description=(
            "When True (default), every IPS decision is still computed, "
            "persisted, and broadcast exactly as normal, but the "
            "enforcement adapter is told this is a simulation and the "
            "pipeline's active-mitigation registry (which flows treat an "
            "asset as already actioned) is updated for bookkeeping only — "
            "no separate code path is skipped, since this environment has "
            "no real network fabric to act on regardless (see "
            "backend/ips/enforcement.py). Independent of ips_enabled: an "
            "operator can leave the layer enabled-but-simulated "
            "indefinitely, which is the requirement's own 'validate first "
            "in dry-run mode' step made a persistent, not one-shot, "
            "posture."
        ),
    )
    ips_min_corroborating_detectors: int = Field(
        default=2,
        ge=1,
        le=5,
        description=(
            "Minimum independently-fired detectors (out of the five "
            "Hybrid IDS channels) required before the IPS layer will "
            "consider ACTIVE prevention (rate-limit/block/quarantine) — "
            "unless the fused decision already carries a "
            "Certainty.CONFIRMED signal (the honeytoken tripwire), which "
            "always corroborates on its own. This is what stops a single "
            "miscalibrated heuristic detector from ever triggering a block "
            "by itself; it is not a threshold and cannot be tuned away by "
            "relaxing threat_score cutoffs alone — see IPSPolicyEngine."
        ),
    )
    ips_rate_limit_min_threat_score: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description=(
            "Fused threat_score at or above which a corroborated decision "
            "qualifies for RATE_LIMIT. Matches hybrid_band_likely by "
            "default (the same score that already clears the existing "
            "Hybrid IDS alert band), not a separately tuned number, so "
            "'the fused layer says LIKELY or above, and it is "
            "corroborated' is what unlocks active prevention at all."
        ),
    )
    ips_block_min_threat_score: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Fused threat_score at or above which a corroborated decision "
            "qualifies for BLOCK (subject also to "
            "ips_block_min_asset_criticality) or QUARANTINE (subject also "
            "to the quarantine-specific criticality and CII floors below). "
            "Matches hybrid_band_confirmed by default."
        ),
    )
    ips_block_min_asset_criticality: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum target-asset criticality (cii_calculator's "
            "criticality map, 0-1) required, alongside "
            "ips_block_min_threat_score, before BLOCK is considered. A "
            "low-value/auto-discovered asset (e.g. an Unresolved_<ip> "
            "node, criticality 0.1) never reaches BLOCK under the default "
            "policy even at threat_score 1.0 — it can still reach "
            "RATE_LIMIT."
        ),
    )
    ips_quarantine_min_asset_criticality: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum target-asset criticality required, alongside "
            "ips_block_min_threat_score and ips_quarantine_min_cii_median, "
            "before QUARANTINE (the strongest, most disruptive tier — "
            "isolating the asset entirely) is considered. Enforced to sit "
            "at or above ips_block_min_asset_criticality by "
            "_check_ips_thresholds_ordered below — quarantine must never "
            "be reachable by an asset that would not already qualify for "
            "the lesser BLOCK action."
        ),
    )
    ips_quarantine_min_cii_median: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum CII median (fraction of the city's total criticality "
            "mass projected to cascade, see cii_calculator.py) required "
            "before QUARANTINE is considered, alongside the criticality "
            "floor above. Guards against isolating an intrinsically "
            "critical asset whose CURRENT projected blast radius is near "
            "zero — e.g. every downstream dependent is already isolated, "
            "or the asset is a structural leaf right now — where isolating "
            "it further would remove an operator's own visibility into it "
            "without containing anything additional."
        ),
    )
    ips_rate_limit_ttl_sec: float = Field(
        default=900.0,
        gt=0.0,
        description=(
            "How long a RATE_LIMIT action stays active before it "
            "automatically expires (the requirement's 'temporary actions "
            "with TTL/expiry'). 15 minutes — long enough to matter during "
            "a live incident, short enough that a stale action from an "
            "old, already-resolved compromise does not linger indefinitely "
            "with no operator involvement."
        ),
    )
    ips_block_ttl_sec: float = Field(
        default=1800.0,
        gt=0.0,
        description=(
            "TTL for a BLOCK action. 30 minutes — longer than "
            "RATE_LIMIT's, reflecting the stronger evidence bar BLOCK "
            "already requires to trigger at all."
        ),
    )
    ips_quarantine_ttl_sec: float = Field(
        default=3600.0,
        gt=0.0,
        description=(
            "TTL for a QUARANTINE action, the most disruptive tier. 1 "
            "hour — long enough that an operator has real time to review "
            "before it auto-expires, short enough that an unreviewed "
            "isolation cannot silently become permanent."
        ),
    )
    ips_active_action_cache_max_entries: int = Field(
        default=2000,
        ge=10,
        le=200_000,
        description=(
            "LRU cap on IngestPipeline's in-memory active-mitigation "
            "registry (keyed by target asset). Same rationale as "
            "cii_cache_max_entries / the alert debounce map: "
            "AssetRegistry auto-registers one node per unique unresolved "
            "IP, so an unbounded registry is an unbounded leak over a long "
            "replay."
        ),
    )

    @model_validator(mode="after")
    def _check_ips_thresholds_ordered(self) -> "BackendSettings":
        """Mirrors `_check_hybrid_bands_ordered` above: an out-of-order IPS
        threshold does not raise anywhere downstream — it just makes a
        tier unreachable or reachable in the wrong place — so it is
        caught here, at construction, instead."""
        if not (
            self.ips_rate_limit_min_threat_score <= self.ips_block_min_threat_score
        ):
            raise ValueError(
                "ips_rate_limit_min_threat_score "
                f"({self.ips_rate_limit_min_threat_score}) must be <= "
                f"ips_block_min_threat_score ({self.ips_block_min_threat_score})"
            )
        if not (
            self.ips_block_min_asset_criticality
            <= self.ips_quarantine_min_asset_criticality
        ):
            raise ValueError(
                "ips_block_min_asset_criticality "
                f"({self.ips_block_min_asset_criticality}) must be <= "
                "ips_quarantine_min_asset_criticality "
                f"({self.ips_quarantine_min_asset_criticality})"
            )
        return self

    @model_validator(mode="after")
    def _check_hybrid_bands_ordered(self) -> "BackendSettings":
        """Band thresholds must be strictly increasing.

        Out-of-order bands do not raise anywhere downstream — they just
        make one band unreachable and silently reclassify traffic, which
        is the kind of misconfiguration that looks like a detector bug.
        Caught here instead, at construction.
        """
        if not (
            self.hybrid_band_suspicious
            < self.hybrid_band_likely
            < self.hybrid_band_confirmed
        ):
            raise ValueError(
                "hybrid band thresholds must be strictly increasing: "
                f"suspicious ({self.hybrid_band_suspicious}) < likely "
                f"({self.hybrid_band_likely}) < confirmed "
                f"({self.hybrid_band_confirmed})"
            )
        return self

    @model_validator(mode="after")
    def _check_beaconing_interval_window_ordered(self) -> "BackendSettings":
        """The interval floor must sit below the ceiling, or every
        interval is excluded and the detector silently never fires."""
        if self.beaconing_min_interval_sec >= self.beaconing_max_interval_sec:
            raise ValueError(
                "beaconing_min_interval_sec "
                f"({self.beaconing_min_interval_sec}) must be < "
                f"beaconing_max_interval_sec ({self.beaconing_max_interval_sec})"
            )
        return self

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

    @property
    def supervised_model_artifact_path_resolved(self) -> Path:
        """`supervised_model_artifact_path`, guaranteed absolute and
        CWD-independent — see `model_artifact_path_resolved`'s docstring
        for the identical rationale."""
        if self.supervised_model_artifact_path.is_absolute():
            return self.supervised_model_artifact_path
        return _REPO_ROOT / self.supervised_model_artifact_path


# Module-level singleton — import this everywhere.
BACKEND_SETTINGS = BackendSettings()
