"""
tests/test_ips_api.py — backend/routes.py's IPS routes:

    GET  /api/ips/policy
    GET  /api/ips/actions
    POST /api/ips/actions/{id}/rollback

Mirrors tests/test_api.py's own fixtures and dependency-override style
exactly (`sqlite_session_scope`, `make_client`) rather than introducing a
parallel test-app convention — reuses that module's fixtures directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.ingest import CollectingBroadcaster, ENVELOPE_IPS_ACTION, IngestPipeline
from backend.models import IpsAction
from backend.runtime import AppRuntime

# `sqlite_session_scope` is a `@pytest.fixture` defined in test_api.py;
# importing it here is the standard pytest cross-module fixture-reuse
# idiom (ruff's F401/F811 don't special-case pytest fixtures, hence the
# blanket noqa -- ruff's tests/ lint is not part of CI regardless, see
# .github/workflows/ci.yml's `ruff check src/ backend/` scope).
from tests.test_api import (  # noqa: F401,F811
    make_client,
    make_no_scorer_runtime,
    sqlite_session_scope,
)


def _insert_action(
    session_scope,
    *,
    target_asset: str = "City_Payment_Gateway",
    action: str = "rate_limit",
    status: str = "simulated",
    confidence: float = 0.7,
    dry_run: bool = True,
) -> int:
    with session_scope() as session:
        row = IpsAction(
            ts=datetime.now(timezone.utc),
            target_asset=target_asset,
            action=action,
            status=status,
            reason="test fixture",
            evidence={"threat_score": confidence},
            confidence=confidence,
            dry_run=dry_run,
        )
        session.add(row)
        session.flush()
        return row.id


def _make_pipeline_runtime(session_scope) -> AppRuntime:
    """A real `IngestPipeline`, wired to the SAME SQLite session factory
    the route's `get_session_scope` override uses, over a REAL
    `ReplayEngine` (`_require_replay_engine` needs `runtime.engine` to
    not be `None` — see `tests.test_api.make_engine_runtime`'s own
    rationale for the identical pairing)."""
    from tests.test_api import BASE_TS, _FakeReader, _flow, _noop_consumer
    from backend.replay_engine import ReplayEngine
    from datetime import timedelta

    pipeline = IngestPipeline(
        scorer=object(),
        broadcaster=CollectingBroadcaster(),
        session_factory=session_scope,
    )
    flows = [_flow(BASE_TS + timedelta(seconds=i), f"synthetic:{i}") for i in range(5)]
    engine = ReplayEngine(
        consumer=_noop_consumer,
        reader=_FakeReader(flows),
        tick_interval=0.01,
        thread_join_timeout=2.0,
    )
    return AppRuntime(
        scorer=object(),
        pipeline=pipeline,
        engine=engine,
        scorer_load_error=None,
        started_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# GET /api/ips/policy
# ---------------------------------------------------------------------------


def test_get_ips_policy_reads_backend_settings():
    from backend.config import BACKEND_SETTINGS

    client = make_client()
    r = client.get("/api/ips/policy")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] == BACKEND_SETTINGS.ips_enabled
    assert body["dry_run"] == BACKEND_SETTINGS.ips_dry_run
    assert body["block_min_threat_score"] == BACKEND_SETTINGS.ips_block_min_threat_score


# ---------------------------------------------------------------------------
# GET /api/ips/actions
# ---------------------------------------------------------------------------


def test_list_ips_actions_returns_inserted_rows(sqlite_session_scope):
    _insert_action(sqlite_session_scope, target_asset="AssetA", action="block", status="enforced")
    _insert_action(sqlite_session_scope, target_asset="AssetB", action="rate_limit", status="simulated")
    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/ips/actions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["actions"]) == 2
    assert {a["target_asset"] for a in body["actions"]} == {"AssetA", "AssetB"}


def test_list_ips_actions_filters_by_target_asset(sqlite_session_scope):
    _insert_action(sqlite_session_scope, target_asset="AssetA")
    _insert_action(sqlite_session_scope, target_asset="AssetB")
    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/ips/actions", params={"target_asset": "AssetA"})
    body = r.json()
    assert len(body["actions"]) == 1
    assert body["actions"][0]["target_asset"] == "AssetA"


def test_list_ips_actions_active_filter_excludes_alert_only_rows(sqlite_session_scope):
    """An `alert`-only row is never "active" even with status=simulated —
    mirrors the same fix applied to `IngestPipeline.rollback_ips_action`
    (an alert-tier decision was never enforced, so it has no active
    state to report)."""
    _insert_action(sqlite_session_scope, target_asset="AlertOnly", action="alert", status="simulated")
    _insert_action(sqlite_session_scope, target_asset="ActiveBlock", action="block", status="enforced")
    _insert_action(
        sqlite_session_scope, target_asset="Terminal", action="rate_limit", status="rolled_back"
    )
    client = make_client(session_scope=sqlite_session_scope)

    r_active = client.get("/api/ips/actions", params={"active": "true"})
    assert {a["target_asset"] for a in r_active.json()["actions"]} == {"ActiveBlock"}

    r_inactive = client.get("/api/ips/actions", params={"active": "false"})
    assert {a["target_asset"] for a in r_inactive.json()["actions"]} == {"AlertOnly", "Terminal"}


def test_list_ips_actions_ordered_newest_first(sqlite_session_scope):
    first_id = _insert_action(sqlite_session_scope, target_asset="First")
    second_id = _insert_action(sqlite_session_scope, target_asset="Second")
    client = make_client(session_scope=sqlite_session_scope)
    r = client.get("/api/ips/actions")
    ids = [a["id"] for a in r.json()["actions"]]
    assert ids.index(second_id) < ids.index(first_id)


# ---------------------------------------------------------------------------
# POST /api/ips/actions/{id}/rollback
# ---------------------------------------------------------------------------


def test_rollback_503_when_scorer_never_loaded():
    client = make_client(runtime=make_no_scorer_runtime())
    r = client.post("/api/ips/actions/1/rollback")
    assert r.status_code == 503


def test_rollback_404_for_unknown_id(sqlite_session_scope):
    runtime = _make_pipeline_runtime(sqlite_session_scope)
    client = make_client(session_scope=sqlite_session_scope, runtime=runtime)
    r = client.post("/api/ips/actions/999999/rollback")
    assert r.status_code == 404


def test_rollback_success_updates_row_and_broadcasts(sqlite_session_scope):
    action_id = _insert_action(sqlite_session_scope, action="block", status="enforced")
    runtime = _make_pipeline_runtime(sqlite_session_scope)
    client = make_client(session_scope=sqlite_session_scope, runtime=runtime)

    r = client.post(f"/api/ips/actions/{action_id}/rollback", json={"reason": "operator override"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rolled_back"
    assert body["rollback_reason"] == "operator override"

    envelopes = runtime.pipeline._broadcaster.of_type(ENVELOPE_IPS_ACTION)
    assert len(envelopes) == 1
    assert envelopes[0]["data"]["status"] == "rolled_back"


def test_rollback_409_when_already_terminal(sqlite_session_scope):
    action_id = _insert_action(sqlite_session_scope, action="block", status="rolled_back")
    runtime = _make_pipeline_runtime(sqlite_session_scope)
    client = make_client(session_scope=sqlite_session_scope, runtime=runtime)
    r = client.post(f"/api/ips/actions/{action_id}/rollback")
    assert r.status_code == 409


def test_rollback_409_for_alert_only_action(sqlite_session_scope):
    action_id = _insert_action(sqlite_session_scope, action="alert", status="simulated")
    runtime = _make_pipeline_runtime(sqlite_session_scope)
    client = make_client(session_scope=sqlite_session_scope, runtime=runtime)
    r = client.post(f"/api/ips/actions/{action_id}/rollback")
    assert r.status_code == 409


def test_rollback_without_body_uses_default_reason(sqlite_session_scope):
    action_id = _insert_action(sqlite_session_scope, action="quarantine", status="simulated")
    runtime = _make_pipeline_runtime(sqlite_session_scope)
    client = make_client(session_scope=sqlite_session_scope, runtime=runtime)
    r = client.post(f"/api/ips/actions/{action_id}/rollback")
    assert r.status_code == 200
    assert r.json()["rollback_reason"] == "manual operator rollback"
