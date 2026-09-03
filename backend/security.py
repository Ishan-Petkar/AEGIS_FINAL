"""
backend/security.py — minimal auth + rate limiting for state-changing
routes (Phase B improvement pass).

Two FastAPI dependencies, meant to be composed on every mutating route
(`POST /api/replay/start|stop|speed`, `POST /api/inject`, `POST
/api/alerts/{id}/ack`) alongside the existing `Depends(get_runtime)` /
`Depends(get_session_scope)` pattern:

- `require_api_token` — a static bearer-token check, a no-op when
  `BACKEND_SETTINGS.api_token` is unset (the default). Read the honest
  security-model caveat on that setting's docstring in `backend/
  config.py` before assuming this is more than it is: a token shipped to
  the browser via `NEXT_PUBLIC_API_TOKEN` is visible in the page's own JS
  bundle, so this stops an unrelated web page or opportunistic scanner
  from finding an open control surface, not a targeted attacker reading
  devtools on the page itself.
- `enforce_rate_limit` — a per-IP sliding-window cap, in-memory, one
  process only (this project runs exactly one backend instance; see
  PLAN_MASTER.md's explicit multi-tenancy deferral).

`RateLimiter` is split out as a small, dependency-free class (no FastAPI
import) so the actual limiting *policy* is unit-testable without spinning
up an app, mirroring `backend/retention.py`'s `_rows_to_prune()` /
`prune_events()` split.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request

from backend.config import BACKEND_SETTINGS


class RateLimiter:
    """Per-key sliding-window request counter. Pure Python, no I/O — a
    dict of key -> list of request timestamps, pruned lazily on each
    check rather than by a background sweep (this project's traffic
    pattern — a handful of mutating calls per demo session, never a high
    request rate — makes lazy pruning strictly simpler than a timer for
    no real cost).

    A single `Lock` guards the whole structure. Mutating routes are
    low-frequency by nature (an operator clicking Inject, not a hot
    path), so one coarse lock is deliberately simpler than per-key
    locking and never becomes a real contention point.
    """

    def __init__(self, max_requests: int, window_sec: float) -> None:
        self._max_requests = max_requests
        self._window_sec = window_sec
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """True and records a hit if `key` is under its limit; False
        (does NOT record a hit — a rejected request should not itself
        count toward the window) otherwise."""
        now = now if now is not None else time.monotonic()
        cutoff = now - self._window_sec
        with self._lock:
            hits = [t for t in self._hits[key] if t > cutoff]
            if len(hits) >= self._max_requests:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def reset(self) -> None:
        """Clears all recorded hits for every key. Test-only escape hatch —
        see `reset_rate_limiter_for_tests()` below for why this exists."""
        with self._lock:
            self._hits.clear()


# Module-level singleton — one process, one limiter, matching
# `BACKEND_SETTINGS`'s own module-level-singleton pattern (`settings.py`,
# `config.py`'s own `BACKEND_SETTINGS`).
_rate_limiter = RateLimiter(
    max_requests=BACKEND_SETTINGS.rate_limit_max_requests,
    window_sec=BACKEND_SETTINGS.rate_limit_window_sec,
)


def reset_rate_limiter_for_tests() -> None:
    """Clears all recorded hits. Process-global state (like `backend.db`'s
    engine/session-factory globals) would otherwise leak across tests
    within the same pytest process — a test file that legitimately
    exercises a mutating route more than `rate_limit_max_requests` times
    would start seeing 429s that have nothing to do with what it's
    actually testing. Call from an autouse fixture, mirroring
    `tests/test_api.py`'s existing `_reset_inject_pool_cache` pattern for
    the same class of problem.
    """
    _rate_limiter.reset()


def require_api_token(request: Request) -> None:
    """FastAPI dependency: 401 if `BACKEND_SETTINGS.api_token` is set and
    the request's `Authorization: Bearer <token>` header does not match.
    A no-op (never raises) when `api_token` is unset — see that setting's
    docstring for why that is the correct default rather than a gap.
    """
    token = BACKEND_SETTINGS.api_token
    if not token:
        return
    header = request.headers.get("authorization", "")
    expected = f"Bearer {token}"
    if header != expected:
        raise HTTPException(status_code=401, detail="missing or invalid API token")


def enforce_rate_limit(request: Request) -> None:
    """FastAPI dependency: 429 if this client IP has exceeded
    `BACKEND_SETTINGS.rate_limit_max_requests` state-changing calls within
    `rate_limit_window_sec`. Keyed on `request.client.host` — this
    project has no reverse proxy in front of it (see `docs/SETUP.md`), so
    that is genuinely the caller's address, not a proxy's.
    """
    client = request.client
    key = client.host if client is not None else "unknown"
    if not _rate_limiter.allow(key):
        raise HTTPException(
            status_code=429,
            detail=(
                f"rate limit exceeded: max {BACKEND_SETTINGS.rate_limit_max_requests} "
                f"requests per {BACKEND_SETTINGS.rate_limit_window_sec:.0f}s to this route class"
            ),
        )
