"""
backend/runtime.py — Phase 5 Ticket #8: process-wide `AppRuntime`.

Wires together the three objects that have a real process lifecycle —
`StreamingScorer`, `IngestPipeline`, `ReplayEngine` — into one dataclass
that `backend/main.py`'s lifespan constructs exactly once and stores on
`app.state.runtime`. See docs/PHASE5_TICKET8_PLAN.md section 3 (decision
D8-1) for the full rationale; the short version:

  * Constructed in the FastAPI **lifespan**, never at import time.
    `backend/main.py` merely builds the `FastAPI` app object and points it
    at this module's `build_runtime` via `lifespan=`; nothing runs until
    an ASGI server (or `TestClient` used as a context manager) actually
    starts the app. This is what keeps `import backend.main` cheap and
    order-independent — no joblib artifact load, no DB connection attempt
    — which tests/test_api.py pins directly.
  * `StreamingScorer.load()` deliberately hard-fails (Invariant B — no
    implicit refit on stream data) if the warmup artifact is missing or
    incompatible. That failure is caught HERE, not re-raised: the API
    still starts, and its read-only routes (`/api/health`, `/api/topology`,
    `/api/events`, `/api/alerts`, `/api/cii/{asset}`) need no model at all
    and keep working. Every replay-control route
    (`/api/replay/start|stop|speed`) checks `runtime.engine is None` and
    answers 503 instead — never a silently no-model stream.
  * `scorer`, `pipeline`, and `engine` are set (or left `None`) TOGETHER:
    there is no such thing as "pipeline without a scorer" or "engine
    without a pipeline" in this process, since `IngestPipeline.__init__`
    itself refuses a `None` scorer (`ValueError`) and `ReplayEngine`'s
    only job is to drive a consumer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from backend.ingest import IngestPipeline
from backend.replay_engine import ReplayEngine
from backend.streaming import StreamingScorer, StreamingScorerError

logger = logging.getLogger(__name__)


@dataclass
class AppRuntime:
    """Process-wide runtime, built once by `build_runtime()` and stored on
    `app.state.runtime`. Routes reach it through the `get_runtime`
    dependency in `backend/routes.py`, which tests override with a fake
    instance instead of exercising the real lifespan.
    """

    scorer: Optional[StreamingScorer]
    pipeline: Optional[IngestPipeline]
    engine: Optional[ReplayEngine]
    scorer_load_error: Optional[str]
    started_at: datetime


def build_runtime() -> AppRuntime:
    """Construct the process-wide runtime. Called exactly once, from
    `backend.main`'s lifespan — see the module docstring.

    Returns an `AppRuntime` with `scorer`/`pipeline`/`engine` all `None`
    and `scorer_load_error` set to the failure message if
    `StreamingScorer.load()` raises `StreamingScorerError` (covers both
    `StreamingScorerArtifactMissing` — no `python -m backend.warmup` build
    yet — and `StreamingScorerIncompatible` — a stale/foreign artifact).
    Any other exception is not caught here: a genuinely unexpected failure
    should fail the process loudly rather than silently degrade into "no
    scorer".
    """
    started_at = datetime.now(timezone.utc)
    try:
        scorer = StreamingScorer.load()
    except StreamingScorerError as exc:
        logger.error(
            "AppRuntime: StreamingScorer failed to load (%s); the API will "
            "start, but every replay-control route will answer 503 until "
            "this is fixed. Build the artifact with: "
            "PYTHONPATH=src venv/bin/python -m backend.warmup",
            exc,
        )
        return AppRuntime(
            scorer=None,
            pipeline=None,
            engine=None,
            scorer_load_error=str(exc),
            started_at=started_at,
        )

    pipeline = IngestPipeline(scorer=scorer)
    engine = ReplayEngine(consumer=pipeline)
    return AppRuntime(
        scorer=scorer,
        pipeline=pipeline,
        engine=engine,
        scorer_load_error=None,
        started_at=started_at,
    )
