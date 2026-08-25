"""
backend/main.py — Phase 5 Ticket #8: FastAPI app factory + lifespan.

    uvicorn backend.main:app --host <BACKEND_SETTINGS.api_host> --port <BACKEND_SETTINGS.api_port>

Importing this module must be cheap and side-effect-free: it must NOT load
the `StreamingScorer` joblib artifact and must NOT open a database
connection (docs/PHASE5_TICKET8_PLAN.md section 3, decision D8-1) — both
of those happen exactly once, inside `lifespan()`, which only runs when an
ASGI server (uvicorn, or `TestClient` used as a context manager) actually
starts the app. `create_app()` itself only builds the `FastAPI` object,
attaches CORS middleware, and mounts the router — none of that touches a
model artifact or Postgres either. `tests/test_api.py` pins this directly.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import BACKEND_SETTINGS
from backend.routes import router
from backend.runtime import AppRuntime, build_runtime

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Builds the process-wide `AppRuntime` exactly once at startup (D8-1)
    and stops the replay engine (idempotent, bounded-wait) on shutdown so a
    reload/redeploy never leaves an orphaned background replay thread.
    """
    runtime: AppRuntime = build_runtime()
    app.state.runtime = runtime
    try:
        yield
    finally:
        if runtime.engine is not None:
            runtime.engine.stop()


def create_app() -> FastAPI:
    """Build the FastAPI app. Cheap and side-effect-free — see the module
    docstring. Called once at import time below to produce the module-level
    `app` uvicorn points at, and again by tests that want a fresh app (and
    therefore fresh, non-leaking `dependency_overrides`) per test.
    """
    app = FastAPI(
        title="AEGIS Phase 5 API",
        description=(
            "Cyber-physical risk analytics backend: replay control, live "
            "events/alerts, topology, and on-demand blast-radius (CII) "
            "queries. See docs/PHASE5_TICKET8_PLAN.md."
        ),
        lifespan=lifespan,
    )
    # D8-4: CORS is required (the Next.js console runs on a different
    # origin/port) and must never default to "*" — these routes include
    # unauthenticated state-changing controls. See
    # BACKEND_SETTINGS.api_cors_origins's docstring.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(BACKEND_SETTINGS.api_cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
