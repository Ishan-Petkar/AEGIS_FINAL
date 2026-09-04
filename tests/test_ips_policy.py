"""
tests/test_ips_policy.py — backend/ips/policy.py (`IPSPolicyEngine`).

Mirrors tests/test_fusion.py's style exactly: plain pytest functions, no
test classes, tiny local helpers for building `FusedDecision`s tersely.
Scope: the POLICY (given a FusedDecision + asset criticality + CII
median, what PreventionAction results) — not the Hybrid IDS layer's own
fusion algorithm (tests/test_fusion.py) and not the IngestPipeline wiring
(tests/test_ingest_ips.py: dedup/escalation, persistence, enforcement).
"""

from __future__ import annotations

import pytest

from backend.config import BackendSettings
from backend.detection.contracts import Certainty, DetectorVerdict, FusedDecision, ResponseAction, ThreatBand
from backend.ips.contracts import PreventionAction
from backend.ips.policy import IPSPolicyEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_verdict(
    detector: str,
    calibrated_score: float = 0.9,
    reliability: float = 0.85,
    certainty: Certainty = Certainty.HEURISTIC,
) -> DetectorVerdict:
    return DetectorVerdict(
        detector=detector,
        fired=True,
        calibrated_score=calibrated_score,
        reliability=reliability,
        certainty=certainty,
    )


def make_fused(
    threat_score: float,
    band: ThreatBand,
    verdicts: tuple[DetectorVerdict, ...] = (),
) -> FusedDecision:
    action = ResponseAction.ALERT if band in (ThreatBand.LIKELY, ThreatBand.CONFIRMED) else ResponseAction.OBSERVE
    return FusedDecision(
        threat_score=threat_score,
        band=band,
        action=action,
        rationale="test fixture",
        verdicts=verdicts,
    )


ONE_DETECTOR = (make_verdict("signature"),)
TWO_DETECTORS = (make_verdict("signature"), make_verdict("beaconing"))
CONFIRMED_ALONE = (
    DetectorVerdict(
        detector="tripwire",
        fired=True,
        calibrated_score=1.0,
        reliability=1.0,
        certainty=Certainty.CONFIRMED,
    ),
)


# ---------------------------------------------------------------------------
# Below-alert bands -> OBSERVE, regardless of anything else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("band", [ThreatBand.BENIGN, ThreatBand.SUSPICIOUS])
def test_below_alert_band_is_always_observe(band):
    engine = IPSPolicyEngine()
    fused = make_fused(0.9, band, TWO_DETECTORS)  # high score, corroborated -- still OBSERVE
    decision = engine.decide(fused, "SomeAsset", asset_criticality=1.0, cii_median=1.0)
    assert decision.action == PreventionAction.OBSERVE


# ---------------------------------------------------------------------------
# Corroboration requirement -- "never block every anomaly automatically"
# ---------------------------------------------------------------------------


def test_single_detector_never_reaches_active_prevention():
    """A single heuristic detector, even at a very high individual score,
    can only ever reach ALERT -- this is the guard no threshold can
    bypass."""
    engine = IPSPolicyEngine()
    fused = make_fused(0.99, ThreatBand.CONFIRMED, ONE_DETECTOR)
    decision = engine.decide(fused, "AssetA", asset_criticality=1.0, cii_median=1.0)
    assert decision.action == PreventionAction.ALERT
    assert not decision.is_active_prevention
    assert "corroboration" in decision.reason


def test_two_detectors_corroborate_without_a_confirmed_signal():
    engine = IPSPolicyEngine()
    fused = make_fused(0.9, ThreatBand.CONFIRMED, TWO_DETECTORS)
    decision = engine.decide(fused, "AssetB", asset_criticality=0.9, cii_median=0.5)
    assert decision.action in (PreventionAction.BLOCK, PreventionAction.QUARANTINE, PreventionAction.RATE_LIMIT)


def test_confirmed_signal_alone_corroborates():
    """A CONFIRMED verdict (the honeytoken tripwire) corroborates on its
    own -- one detector is enough when it cannot false-positive."""
    engine = IPSPolicyEngine()
    fused = make_fused(1.0, ThreatBand.CONFIRMED, CONFIRMED_ALONE)
    decision = engine.decide(fused, "AssetC", asset_criticality=0.9, cii_median=0.5)
    assert decision.action != PreventionAction.ALERT
    assert decision.is_active_prevention


def test_corroboration_threshold_is_configurable():
    engine = IPSPolicyEngine(min_corroborating_detectors=3)
    fused = make_fused(0.9, ThreatBand.CONFIRMED, TWO_DETECTORS)  # only 2 fired
    decision = engine.decide(fused, "AssetD", asset_criticality=0.9, cii_median=0.5)
    assert decision.action == PreventionAction.ALERT


# ---------------------------------------------------------------------------
# Tier selection, once corroborated
# ---------------------------------------------------------------------------


def test_rate_limit_when_corroborated_but_below_block_threshold():
    engine = IPSPolicyEngine()
    fused = make_fused(0.6, ThreatBand.LIKELY, TWO_DETECTORS)  # below block_min_threat_score (0.85)
    decision = engine.decide(fused, "AssetE", asset_criticality=0.9, cii_median=0.9)
    assert decision.action == PreventionAction.RATE_LIMIT
    assert decision.ttl_sec == pytest.approx(BackendSettings().ips_rate_limit_ttl_sec)


def test_rate_limit_when_high_score_but_low_criticality():
    """High threat_score alone is not enough for BLOCK -- the target
    asset must also clear the criticality floor."""
    engine = IPSPolicyEngine()
    fused = make_fused(0.95, ThreatBand.CONFIRMED, TWO_DETECTORS)
    decision = engine.decide(fused, "LowValueAsset", asset_criticality=0.1, cii_median=0.9)
    assert decision.action == PreventionAction.RATE_LIMIT


def test_block_when_corroborated_high_score_and_sufficient_criticality():
    engine = IPSPolicyEngine()
    fused = make_fused(0.9, ThreatBand.CONFIRMED, TWO_DETECTORS)
    decision = engine.decide(fused, "MidCritAsset", asset_criticality=0.6, cii_median=0.02)
    assert decision.action == PreventionAction.BLOCK
    assert decision.ttl_sec == pytest.approx(BackendSettings().ips_block_ttl_sec)


def test_quarantine_needs_criticality_and_real_blast_radius_together():
    engine = IPSPolicyEngine()
    fused = make_fused(0.9, ThreatBand.CONFIRMED, TWO_DETECTORS)

    # High criticality alone, but no real blast radius -> BLOCK, not QUARANTINE.
    no_cii = engine.decide(fused, "CriticalButIsolated", asset_criticality=0.95, cii_median=0.0)
    assert no_cii.action == PreventionAction.BLOCK

    # cii_median omitted entirely (None) -> same guard, still BLOCK.
    no_cii_arg = engine.decide(fused, "CriticalButIsolated", asset_criticality=0.95, cii_median=None)
    assert no_cii_arg.action == PreventionAction.BLOCK

    # Both criticality AND real projected impact -> QUARANTINE.
    both = engine.decide(fused, "CriticalAndConnected", asset_criticality=0.95, cii_median=0.3)
    assert both.action == PreventionAction.QUARANTINE
    assert both.ttl_sec == pytest.approx(BackendSettings().ips_quarantine_ttl_sec)


def test_quarantine_min_asset_criticality_is_at_or_above_block_floor():
    """Config invariant, not just a policy behavior: quarantine must never
    be reachable by an asset that would not already qualify for BLOCK —
    pinned here as a settings-level fact, mirroring
    _check_ips_thresholds_ordered's own purpose."""
    s = BackendSettings()
    assert s.ips_block_min_asset_criticality <= s.ips_quarantine_min_asset_criticality


# ---------------------------------------------------------------------------
# Confidence, evidence, decision shape
# ---------------------------------------------------------------------------


def test_decision_confidence_matches_fused_threat_score():
    engine = IPSPolicyEngine()
    fused = make_fused(0.73, ThreatBand.LIKELY, TWO_DETECTORS)
    decision = engine.decide(fused, "AssetF", asset_criticality=0.5, cii_median=0.5)
    assert decision.confidence == pytest.approx(0.73)


def test_evidence_carries_fired_detector_names_and_context():
    engine = IPSPolicyEngine()
    fused = make_fused(0.9, ThreatBand.CONFIRMED, TWO_DETECTORS)
    decision = engine.decide(fused, "AssetG", asset_criticality=0.6, cii_median=0.02)
    assert set(decision.evidence["fired_detectors"]) == {"signature", "beaconing"}
    assert decision.evidence["n_fired"] == 2
    assert decision.evidence["corroborated"] is True
    assert decision.evidence["asset_criticality"] == 0.6
    assert decision.evidence["cii_median"] == 0.02


def test_observe_decisions_carry_no_ttl():
    engine = IPSPolicyEngine()
    fused = make_fused(0.1, ThreatBand.BENIGN)
    decision = engine.decide(fused, "QuietAsset", asset_criticality=0.5, cii_median=0.0)
    assert decision.ttl_sec is None


def test_alert_decisions_carry_no_ttl():
    engine = IPSPolicyEngine()
    fused = make_fused(0.6, ThreatBand.LIKELY, ONE_DETECTOR)
    decision = engine.decide(fused, "AssetH", asset_criticality=0.5, cii_median=0.0)
    assert decision.action == PreventionAction.ALERT
    assert decision.ttl_sec is None


def test_prevention_decision_rejects_out_of_range_confidence():
    from backend.ips.contracts import PreventionDecision

    with pytest.raises(ValueError):
        PreventionDecision(
            action=PreventionAction.BLOCK,
            target_asset="x",
            confidence=1.5,
            reason="bad",
        )


# ---------------------------------------------------------------------------
# Determinism / purity — the policy engine must be a pure function
# ---------------------------------------------------------------------------


def test_decide_is_pure_same_input_same_output():
    engine = IPSPolicyEngine()
    fused = make_fused(0.9, ThreatBand.CONFIRMED, TWO_DETECTORS)
    first = engine.decide(fused, "AssetI", asset_criticality=0.7, cii_median=0.05)
    second = engine.decide(fused, "AssetI", asset_criticality=0.7, cii_median=0.05)
    assert first == second
