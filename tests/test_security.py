"""
tests/test_security.py — backend/security.py (Phase B improvement pass:
minimal bearer-token auth + per-IP rate limiting on state-changing routes).

Two layers, tested separately per this repo's usual split (mirrors
`backend/retention.py`'s pure-policy / DB-touching split):

  `RateLimiter` — pure Python, no FastAPI, no monkeypatching of real time
                  needed (an injected `now` makes the window boundary
                  exact and deterministic).
  API-level     — `require_api_token` / `enforce_rate_limit` wired onto a
                  real route via `TestClient`, proving the dependency
                  actually gates the request (not just that the function
                  exists). Reuses `tests/test_api.py`'s own `make_client`
                  / `make_no_scorer_runtime` helpers rather than
                  duplicating that plumbing.

`BACKEND_SETTINGS` is a frozen pydantic model (see
`tests/test_backend_config.py::test_backend_settings_importable_and_frozen`)
— `monkeypatch.setattr(BACKEND_SETTINGS, "api_token", ...)` would raise
`ValidationError: Instance is frozen`. Tests that need a token configured
instead build a fresh `BackendSettings(api_token=...)` instance and
monkeypatch the NAME `backend.security.BACKEND_SETTINGS` is imported as,
swapping the whole binding rather than mutating the frozen object —
standard practice for a frozen settings singleton.
"""

from __future__ import annotations

import pytest

import backend.security as security_module
from backend.config import BackendSettings
from backend.security import RateLimiter, reset_rate_limiter_for_tests
from tests.test_api import make_client, make_no_scorer_runtime


# ---------------------------------------------------------------------------
# RateLimiter — pure unit tests, no app/DB
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_up_to_the_max_then_blocks():
    limiter = RateLimiter(max_requests=3, window_sec=60.0)
    assert limiter.allow("k", now=0.0) is True
    assert limiter.allow("k", now=1.0) is True
    assert limiter.allow("k", now=2.0) is True
    assert limiter.allow("k", now=3.0) is False  # 4th within the window


def test_rate_limiter_rejected_call_does_not_itself_count():
    """A 429'd request must not consume a slot — otherwise a client that's
    already blocked could never recover once traffic resumes within the
    window."""
    limiter = RateLimiter(max_requests=1, window_sec=60.0)
    assert limiter.allow("k", now=0.0) is True
    assert limiter.allow("k", now=1.0) is False
    assert limiter.allow("k", now=2.0) is False
    # Still only ever recorded the one real hit.
    assert len(limiter._hits["k"]) == 1


def test_rate_limiter_window_slides_and_old_hits_expire():
    limiter = RateLimiter(max_requests=2, window_sec=10.0)
    assert limiter.allow("k", now=0.0) is True
    assert limiter.allow("k", now=1.0) is True
    assert limiter.allow("k", now=2.0) is False  # 3rd within the 10s window
    # Past the window from the first two hits — both have expired.
    assert limiter.allow("k", now=11.0) is True
    assert limiter.allow("k", now=11.5) is True
    assert limiter.allow("k", now=11.9) is False


def test_rate_limiter_keys_are_independent():
    limiter = RateLimiter(max_requests=1, window_sec=60.0)
    assert limiter.allow("a", now=0.0) is True
    assert limiter.allow("b", now=0.0) is True  # different key, own budget
    assert limiter.allow("a", now=1.0) is False
    assert limiter.allow("b", now=1.0) is False


def test_reset_clears_every_key():
    limiter = RateLimiter(max_requests=1, window_sec=60.0)
    limiter.allow("k", now=0.0)
    limiter.reset()
    assert limiter.allow("k", now=0.5) is True


# ---------------------------------------------------------------------------
# API-level: the dependencies actually gate a real route
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    reset_rate_limiter_for_tests()
    yield
    reset_rate_limiter_for_tests()


def _client_with_token(token: str | None):
    settings = BackendSettings(api_token=token)
    return settings


def test_mutating_route_works_with_no_token_configured():
    """Default posture (AEGIS_API_TOKEN unset): unchanged from before this
    feature — no Authorization header required."""
    client = make_client(runtime=make_no_scorer_runtime())
    resp = client.post("/api/replay/stop")
    assert resp.status_code == 200


def test_mutating_route_requires_token_once_configured(monkeypatch):
    monkeypatch.setattr(security_module, "BACKEND_SETTINGS", _client_with_token("secret123"))
    client = make_client(runtime=make_no_scorer_runtime())
    resp = client.post("/api/replay/stop")
    assert resp.status_code == 401


def test_mutating_route_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(security_module, "BACKEND_SETTINGS", _client_with_token("secret123"))
    client = make_client(runtime=make_no_scorer_runtime())
    resp = client.post("/api/replay/stop", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_mutating_route_accepts_correct_token(monkeypatch):
    monkeypatch.setattr(security_module, "BACKEND_SETTINGS", _client_with_token("secret123"))
    client = make_client(runtime=make_no_scorer_runtime())
    resp = client.post("/api/replay/stop", headers={"Authorization": "Bearer secret123"})
    assert resp.status_code == 200


def test_get_routes_never_require_a_token(monkeypatch):
    """Only state-changing routes are gated — `GET /api/health` stays
    open even with a token configured, so a monitoring probe never needs
    one."""
    monkeypatch.setattr(security_module, "BACKEND_SETTINGS", _client_with_token("secret123"))
    client = make_client(runtime=make_no_scorer_runtime())
    resp = client.get("/api/health")
    assert resp.status_code != 401


def test_mutating_route_429s_past_the_configured_limit(monkeypatch):
    # `enforce_rate_limit` closes over the module-level `_rate_limiter`
    # singleton, built once at import time — swapped wholesale here
    # rather than trying to reconfigure it in place.
    monkeypatch.setattr(
        security_module, "_rate_limiter", RateLimiter(max_requests=2, window_sec=60.0)
    )
    client = make_client(runtime=make_no_scorer_runtime())
    assert client.post("/api/replay/stop").status_code == 200
    assert client.post("/api/replay/stop").status_code == 200
    resp = client.post("/api/replay/stop")
    assert resp.status_code == 429
    assert "rate limit" in resp.json()["detail"].lower()


def test_rejected_request_does_not_consume_rate_limit_budget(monkeypatch):
    """A 401 (bad/missing token) should not itself eat into the rate
    limit budget any more than a 429 should — dependency order matters:
    `require_api_token` runs before `enforce_rate_limit` in
    `_MUTATING_ROUTE_DEPS`, so an unauthenticated flood cannot exhaust a
    legitimate caller's budget."""
    monkeypatch.setattr(security_module, "BACKEND_SETTINGS", _client_with_token("secret123"))
    monkeypatch.setattr(
        security_module, "_rate_limiter", RateLimiter(max_requests=1, window_sec=60.0)
    )
    client = make_client(runtime=make_no_scorer_runtime())
    for _ in range(3):
        resp = client.post("/api/replay/stop")
        assert resp.status_code == 401
    # The one real slot is still available for a correctly-authenticated call.
    resp = client.post("/api/replay/stop", headers={"Authorization": "Bearer secret123"})
    assert resp.status_code == 200
