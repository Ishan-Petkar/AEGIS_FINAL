"""
tests/test_ingest_ips.py — IPS (backend/ips/) integration: `IngestPipeline`
wiring to `backend/ips/policy.py` and `backend/ips/enforcement.py`.

Reuses `tests/test_ingest.py`'s fakes (`FakeScorer`, `FakeSession`,
`make_flow`, `make_pipeline`, `make_meta`) and
`tests/test_ingest_hybrid.py`'s stub detectors (`_AlwaysFireDetector`,
`_NeverFireDetector`, `_FakeClock`) — same cross-file reuse pattern
`test_ingest_hybrid.py` itself uses against `test_ingest.py`.

Scope: this file tests the WIRING (does `IngestPipeline` correctly
compute an IPS decision from a fused decision, dedupe/escalate against
the active-mitigation registry, enforce via the adapter, persist an
`IpsAction` row, broadcast an `ips_action` envelope, expire on TTL, and
roll back) — not the policy algorithm itself
(tests/test_ips_policy.py) and not the enforcement adapter's own
contract (tests/test_ips_enforcement.py).
"""

from __future__ import annotations

from backend.detection.contracts import DETECTOR_BEACONING, DETECTOR_SIGNATURE
from backend.ingest import ENVELOPE_IPS_ACTION, CollectingBroadcaster, IngestPipeline
from backend.ips.contracts import (
    ActionStatus,
    EnforcementResult,
    PreventionAction,
    PreventionDecision,
)
from backend.ips.policy import IPSPolicyEngine
from tests.test_ingest import FakeScorer, make_flow, make_meta, make_pipeline
from tests.test_ingest_hybrid import _AlwaysFireDetector, _FakeClock, _NeverFireDetector

ORIGIN_ASSET = "City_Payment_Gateway"  # what GRAPH_ASSET_IP resolves to (see test_ingest.py)


def _corroborated_pipeline(
    sig_score: float = 0.95,
    sig_reliability: float = 1.0,
    beacon_score: float = 0.9,
    beacon_reliability: float = 1.0,
    **kw,
):
    """A pipeline whose hybrid layer fires TWO independent detectors
    (signature + beaconing) on every flow -- clears IPSPolicyEngine's
    default corroboration bar (2) without relying on a CONFIRMED signal,
    the same "escalating" shape test_ingest_hybrid.py's own
    `_escalating_pipeline` uses for the alert-gating tests. Defaults
    (0.95/1.0, 0.9/1.0) produce a noisy-OR threat_score around 0.995 --
    comfortably CONFIRMED-band and, against GRAPH_ASSET_IP's real
    criticality (0.95, well above every default threshold), reaches
    BLOCK or QUARANTINE. Pass weaker scores for a test that needs to
    stay at RATE_LIMIT -- see test_active_action_expires_after_its_ttl.

    NOTE: asset criticality here is whatever `AssetRegistry.from_config()`
    already assigns GRAPH_ASSET_IP's resolved asset (real static config,
    not the CII engine's separate `criticality_map` -- passing that
    kwarg does NOT change `IPSPolicyEngine.decide()`'s `asset_criticality`
    argument, which comes from `AssetRegistry.resolve().criticality`).
    """
    return make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: False,
        signature_engine=_AlwaysFireDetector(
            DETECTOR_SIGNATURE, calibrated_score=sig_score, reliability=sig_reliability
        ),
        beaconing_detector=_AlwaysFireDetector(
            DETECTOR_BEACONING, calibrated_score=beacon_score, reliability=beacon_reliability
        ),
        **kw,
    )


class _RaisingEnforcementAdapter:
    def apply(self, decision, dry_run: bool = True):
        raise RuntimeError("adapter exploded")

    def rollback(self, target_asset, action):
        raise RuntimeError("adapter exploded")


class _RecordingEnforcementAdapter:
    """A real (non-raising) adapter that records every call, so a test
    can assert on exactly what the pipeline asked it to do."""

    def __init__(self):
        self.applied: list[tuple[PreventionDecision, bool]] = []
        self.rolled_back: list[tuple[str, PreventionAction]] = []

    def apply(self, decision, dry_run: bool = True):
        self.applied.append((decision, dry_run))
        status = ActionStatus.SIMULATED if dry_run else ActionStatus.ENFORCED
        return EnforcementResult(status=status, detail="recorded")

    def rollback(self, target_asset, action):
        self.rolled_back.append((target_asset, action))
        return EnforcementResult(status=ActionStatus.ROLLED_BACK, detail="recorded")


# ---------------------------------------------------------------------------
# ips_enabled=False (default) is a complete no-op
# ---------------------------------------------------------------------------


def test_ips_disabled_by_default_no_rows_no_envelope_no_counters():
    broadcaster = CollectingBroadcaster()
    pipeline, session = _corroborated_pipeline(broadcaster=broadcaster)  # ips_enabled defaults False
    result = pipeline([make_flow("f:1")], make_meta())
    assert session.ips_actions() == []
    assert broadcaster.of_type(ENVELOPE_IPS_ACTION) == []
    assert result.ips_decisions == 0
    assert pipeline.active_ips_actions() == {}


def test_ips_requires_hybrid_enabled_even_if_ips_itself_is_on():
    """The IPS layer consumes Hybrid IDS output structurally (backend/ips/
    policy.py's own docstring) -- with hybrid_enabled=False,
    fused_decisions is None and IPS must not run at all, regardless of
    ips_enabled."""
    pipeline, session = _corroborated_pipeline(ips_enabled=True, hybrid_enabled=False)
    result = pipeline([make_flow("f:1")], make_meta())
    assert session.ips_actions() == []
    assert result.ips_decisions == 0


# ---------------------------------------------------------------------------
# A corroborated decision persists, dry-run by default
# ---------------------------------------------------------------------------


def test_corroborated_decision_persists_and_broadcasts():
    broadcaster = CollectingBroadcaster()
    pipeline, session = _corroborated_pipeline(ips_enabled=True, broadcaster=broadcaster)
    result = pipeline([make_flow("f:1")], make_meta())

    actions = session.ips_actions()
    assert len(actions) == 1
    row = actions[0]
    assert row.target_asset == ORIGIN_ASSET
    assert row.action in (
        PreventionAction.RATE_LIMIT.value,
        PreventionAction.BLOCK.value,
        PreventionAction.QUARANTINE.value,
    )
    # Dry-run is the shipped default -- see BACKEND_SETTINGS.ips_dry_run.
    assert row.status == ActionStatus.SIMULATED.value
    assert row.dry_run is True
    assert row.evidence["fired_detectors"] == ["signature", "beaconing"] or set(
        row.evidence["fired_detectors"]
    ) == {"signature", "beaconing"}

    [envelope] = broadcaster.of_type(ENVELOPE_IPS_ACTION)
    assert envelope["data"]["target_asset"] == ORIGIN_ASSET
    assert envelope["data"]["status"] == "simulated"
    assert result.ips_decisions == 1
    assert result.ips_actions_simulated == 1


def test_dry_run_false_enforces():
    pipeline, session = _corroborated_pipeline(ips_enabled=True, ips_dry_run=False)
    result = pipeline([make_flow("f:1")], make_meta())
    [row] = session.ips_actions()
    assert row.status == ActionStatus.ENFORCED.value
    assert row.dry_run is False
    assert result.ips_actions_enforced == 1


def test_uncorroborated_single_detector_persists_alert_only():
    """A single fired detector never reaches active prevention (see
    tests/test_ips_policy.py) -- the pipeline should still persist the
    resulting ALERT-tier PreventionDecision (the requirement's "record
    every prevention decision"), but never touch the active-mitigation
    registry."""
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: False,
        signature_engine=_AlwaysFireDetector(DETECTOR_SIGNATURE, calibrated_score=0.95),
        beaconing_detector=_NeverFireDetector(DETECTOR_BEACONING),
        ips_enabled=True,
    )
    pipeline([make_flow("f:1")], make_meta())
    [row] = session.ips_actions()
    assert row.action == PreventionAction.ALERT.value
    assert pipeline.active_ips_actions() == {}


# ---------------------------------------------------------------------------
# Duplicate/conflicting-action protection
# ---------------------------------------------------------------------------


def test_duplicate_decision_on_same_asset_is_suppressed_not_repersisted():
    pipeline, session = _corroborated_pipeline(ips_enabled=True)
    r1 = pipeline([make_flow("f:1")], make_meta(batch_index=1))
    r2 = pipeline([make_flow("f:2")], make_meta(batch_index=2))
    assert r1.ips_decisions == 1
    assert r2.ips_decisions == 0
    assert r2.ips_actions_duplicate_suppressed == 1
    # Still exactly one row -- the second identical decision never
    # touched the database.
    assert len(session.ips_actions()) == 1


def test_escalation_supersedes_the_lower_severity_row():
    """First batch: signature + beaconing both fire, but modestly --
    threat_score lands well inside the RATE_LIMIT band (below the
    default 0.85 block threshold). Second batch: a much hotter beaconing
    verdict pushes the SAME asset's fused score past the block threshold
    -- the earlier RATE_LIMIT row must be marked SUPERSEDED, not
    silently overwritten, and the registry must now point at the new
    row. `quarantine_min_cii_median` is set unreachable so the escalated
    tier is deterministically BLOCK, independent of this asset's actual
    (real, computed) CII median."""
    policy = IPSPolicyEngine(quarantine_min_cii_median=1.1)
    pipeline, session = _corroborated_pipeline(
        sig_score=0.75, sig_reliability=0.8,  # p=0.60
        beacon_score=0.5, beacon_reliability=0.6,  # p=0.30 -> threat_score 1-(0.4*0.7)=0.72
        ips_enabled=True,
        policy_engine=policy,
    )
    r1 = pipeline([make_flow("f:1")], make_meta(batch_index=1))
    assert r1.ips_decisions == 1
    [first_row] = session.ips_actions()
    assert first_row.action == PreventionAction.RATE_LIMIT.value
    first_id = first_row.id

    # Swap in a much hotter beaconing stub so the SAME pipeline's next
    # batch corroborates strongly enough to escalate to BLOCK:
    # threat_score = 1-(1-0.60)(1-0.97) = 1-(0.40*0.03) = 0.988.
    pipeline._beaconing_detector = _AlwaysFireDetector(
        DETECTOR_BEACONING, calibrated_score=0.97, reliability=1.0
    )
    r2 = pipeline([make_flow("f:2")], make_meta(batch_index=2))

    assert r2.ips_decisions == 1
    assert r2.ips_actions_escalated == 1
    rows = session.ips_actions()
    assert len(rows) == 2
    superseded = next(r for r in rows if r.id == first_id)
    assert superseded.status == ActionStatus.SUPERSEDED.value
    assert superseded.rolled_back_at is not None
    assert superseded.rollback_reason.startswith("superseded by action")
    new_row = next(r for r in rows if r.id != first_id)
    assert new_row.action == PreventionAction.BLOCK.value
    assert pipeline.active_ips_actions()[ORIGIN_ASSET] == new_row.action


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


def test_active_action_expires_after_its_ttl():
    clock = _FakeClock(start=0.0)
    policy = IPSPolicyEngine(rate_limit_ttl_sec=10.0)
    pipeline, session = _corroborated_pipeline(
        # Weak enough that threat_score (0.72) stays below the default
        # block threshold (0.85) -- guarantees RATE_LIMIT, not BLOCK,
        # regardless of this asset's real criticality/CII.
        sig_score=0.75, sig_reliability=0.8,
        beacon_score=0.5, beacon_reliability=0.6,
        ips_enabled=True,
        policy_engine=policy,
        clock=clock,
    )
    pipeline([make_flow("f:1")], make_meta(batch_index=1))
    [row] = session.ips_actions()
    assert row.action == PreventionAction.RATE_LIMIT.value
    assert pipeline.active_ips_actions() == {ORIGIN_ASSET: "rate_limit"}

    clock.advance(11.0)  # past the 10s TTL
    # A second, unrelated batch on a DIFFERENT asset still runs the TTL
    # sweep every batch (see _maybe_expire_ips_actions's docstring) --
    # that second batch also corroborates on its OWN asset (the stub
    # detectors fire on every flow) and gets its own fresh registry
    # entry, so the assertion below is "the ORIGINAL asset's entry is
    # gone", not "the registry is empty".
    pipeline([make_flow("f:2", src_ip="10.0.1.99")], make_meta(batch_index=2))

    assert ORIGIN_ASSET not in pipeline.active_ips_actions()
    refreshed = next(r for r in session.ips_actions() if r.id == row.id)
    assert refreshed.status == ActionStatus.EXPIRED.value
    assert refreshed.rolled_back_at is not None
    assert refreshed.rollback_reason == "TTL expired"


def test_ttl_expiry_broadcasts_an_envelope():
    clock = _FakeClock(start=0.0)
    policy = IPSPolicyEngine(rate_limit_ttl_sec=5.0)
    broadcaster = CollectingBroadcaster()
    pipeline, session = _corroborated_pipeline(
        sig_score=0.75, sig_reliability=0.8,
        beacon_score=0.5, beacon_reliability=0.6,
        ips_enabled=True, policy_engine=policy, clock=clock, broadcaster=broadcaster,
    )
    pipeline([make_flow("f:1")], make_meta(batch_index=1))
    clock.advance(6.0)
    pipeline([make_flow("f:2", src_ip="10.0.1.99")], make_meta(batch_index=2))

    expired_envelopes = [
        e for e in broadcaster.of_type(ENVELOPE_IPS_ACTION) if e["data"]["status"] == "expired"
    ]
    assert len(expired_envelopes) == 1


# ---------------------------------------------------------------------------
# Fail-open enforcement
# ---------------------------------------------------------------------------


def test_enforcement_adapter_exception_fails_open_never_raises():
    pipeline, session = _corroborated_pipeline(
        ips_enabled=True, enforcement_adapter=_RaisingEnforcementAdapter()
    )
    # Must not raise -- fail-open per the requirement.
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.ips_actions_failed == 1
    [row] = session.ips_actions()
    assert row.status == ActionStatus.FAILED.value


def test_injected_enforcement_adapter_receives_the_decision_and_dry_run_flag():
    adapter = _RecordingEnforcementAdapter()
    pipeline, session = _corroborated_pipeline(
        ips_enabled=True, ips_dry_run=False, enforcement_adapter=adapter
    )
    pipeline([make_flow("f:1")], make_meta())
    assert len(adapter.applied) == 1
    decision, dry_run = adapter.applied[0]
    assert decision.target_asset == ORIGIN_ASSET
    assert dry_run is False


# ---------------------------------------------------------------------------
# Manual rollback
# ---------------------------------------------------------------------------


def test_rollback_ips_action_marks_row_and_clears_registry():
    adapter = _RecordingEnforcementAdapter()
    pipeline, session = _corroborated_pipeline(
        ips_enabled=True, enforcement_adapter=adapter
    )
    pipeline([make_flow("f:1")], make_meta())
    [row] = session.ips_actions()
    assert pipeline.active_ips_actions() != {}

    envelope = pipeline.rollback_ips_action(row.id, reason="operator says so")
    assert envelope is not None
    assert envelope["data"]["status"] == "rolled_back"
    assert row.rollback_reason == "operator says so"
    assert pipeline.active_ips_actions() == {}
    assert len(adapter.rolled_back) == 1


def test_rollback_unknown_id_returns_none():
    pipeline, session = _corroborated_pipeline(ips_enabled=True)
    pipeline([make_flow("f:1")], make_meta())
    assert pipeline.rollback_ips_action(999_999) is None


def test_rollback_already_terminal_action_returns_none():
    pipeline, session = _corroborated_pipeline(ips_enabled=True)
    pipeline([make_flow("f:1")], make_meta())
    [row] = session.ips_actions()
    first = pipeline.rollback_ips_action(row.id)
    assert first is not None
    second = pipeline.rollback_ips_action(row.id)
    assert second is None


def test_rollback_refuses_an_alert_only_decision():
    """An ALERT-tier decision was never added to the active-mitigation
    registry (it is not active prevention) -- rolling it back must be
    refused, not silently "succeed" against nothing."""
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: False,
        signature_engine=_AlwaysFireDetector(DETECTOR_SIGNATURE, calibrated_score=0.95),
        beaconing_detector=_NeverFireDetector(DETECTOR_BEACONING),
        ips_enabled=True,
    )
    pipeline([make_flow("f:1")], make_meta())
    [row] = session.ips_actions()
    assert row.action == PreventionAction.ALERT.value
    assert pipeline.rollback_ips_action(row.id) is None


# ---------------------------------------------------------------------------
# Default construction wires the real policy engine and adapter
# ---------------------------------------------------------------------------


def test_default_construction_builds_real_ips_layer():
    pipeline = IngestPipeline(scorer=FakeScorer())
    assert isinstance(pipeline._policy_engine, IPSPolicyEngine)
    from backend.ips.enforcement import SimulatedEnforcementAdapter

    assert isinstance(pipeline._enforcement_adapter, SimulatedEnforcementAdapter)
