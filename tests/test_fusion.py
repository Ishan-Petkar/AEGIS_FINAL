"""
tests/test_fusion.py — backend/detection/fusion.py (`HybridFusionEngine`).

Mirrors `tests/test_security.py`'s style: plain pytest functions, no test
classes, no fixtures beyond tiny local helper functions for building
`DetectorVerdict`s tersely.

The single most important test in this file is
`test_confirmed_signal_survives_dilution_by_weak_verdicts` — it pins the
P5-15 guarantee (a confirmed honeytoken touch alerts unconditionally,
never diluted by however many weak volumetric signals accompany it) at
the fusion-engine boundary, independent of `fuse_tripwire_confidence`
which pins the same guarantee at the older 2-channel layer.
"""

from __future__ import annotations

import pytest

from backend.config import BackendSettings
from backend.detection.contracts import (
    Certainty,
    DetectorVerdict,
    FusedDecision,
    ResponseAction,
    ThreatBand,
    verdict_from_tripwire,
)
from backend.detection.fusion import HybridFusionEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_verdict(
    detector: str,
    fired: bool,
    calibrated_score: float,
    reliability: float,
    certainty: Certainty = Certainty.HEURISTIC,
) -> DetectorVerdict:
    return DetectorVerdict(
        detector=detector,
        fired=fired,
        calibrated_score=calibrated_score,
        reliability=reliability,
        certainty=certainty,
    )


def confirmed_verdict(detector: str = "tripwire", fired: bool = True) -> DetectorVerdict:
    """Build a real tripwire verdict via the contract's own adapter (rather
    than hand-rolling one), so its `evidence` shape matches what
    `HybridFusionEngine`'s rationale text actually keys off in
    production."""
    return verdict_from_tripwire(
        fired=fired, tripwire_score=1.0 if fired else 0.0, reliability=1.0, detector=detector
    )


def weak_volumetric(fired: bool, score: float = 1.0) -> DetectorVerdict:
    """The measured ~0.02-precision channel: high calibrated_score,
    tiny reliability."""
    return make_verdict("volumetric", fired=fired, calibrated_score=score, reliability=0.02)


ENGINE = HybridFusionEngine()


# ---------------------------------------------------------------------------
# Step 1 — confirmed-signal precedence
# ---------------------------------------------------------------------------


def test_confirmed_signal_gives_max_score_band_and_alert():
    decision = ENGINE.fuse([confirmed_verdict()])
    assert decision.threat_score == 1.0
    assert decision.band is ThreatBand.CONFIRMED
    assert decision.action is ResponseAction.ALERT
    assert "confirmed" in decision.rationale.lower()
    assert "honeytoken" in decision.rationale.lower()


def test_confirmed_signal_survives_dilution_by_weak_nonfired_verdicts():
    """The dilution guard: a confirmed honeytoken touch plus several weak
    NON-firing heuristic verdicts still yields exactly 1.0 / CONFIRMED /
    ALERT."""
    verdicts = [
        confirmed_verdict(),
        weak_volumetric(fired=False),
        make_verdict("supervised", fired=False, calibrated_score=0.1, reliability=0.9),
        make_verdict("signature", fired=False, calibrated_score=0.05, reliability=0.85),
    ]
    decision = ENGINE.fuse(verdicts)
    assert decision.threat_score == 1.0
    assert decision.band is ThreatBand.CONFIRMED
    assert decision.action is ResponseAction.ALERT


def test_confirmed_signal_survives_dilution_by_weak_fired_verdicts():
    """The dilution guard, harder case: the accompanying weak verdicts
    also FIRED (a ~0.02-precision channel screaming alongside a real
    compromise). Must still be exactly 1.0 / CONFIRMED / ALERT — a
    fired-but-weak channel must never pull a confirmed signal down."""
    verdicts = [
        confirmed_verdict(),
        weak_volumetric(fired=True, score=1.0),
        make_verdict("beaconing", fired=True, calibrated_score=0.9, reliability=0.5),
    ]
    decision = ENGINE.fuse(verdicts)
    assert decision.threat_score == 1.0
    assert decision.band is ThreatBand.CONFIRMED
    assert decision.action is ResponseAction.ALERT


def test_confirmed_but_not_fired_does_not_trigger_precedence():
    """A CONFIRMED-capable detector that did NOT fire (e.g. tripwire with
    no honeytoken touch) must not short-circuit fusion — Step 1 requires
    both fired=True AND certainty CONFIRMED."""
    verdicts = [
        confirmed_verdict(fired=False),
        make_verdict("supervised", fired=True, calibrated_score=0.9, reliability=0.9),
    ]
    decision = ENGINE.fuse(verdicts)
    assert decision.threat_score == pytest.approx(0.9 * 0.9)
    assert decision.threat_score < 1.0
    assert decision.band is not ThreatBand.CONFIRMED


# ---------------------------------------------------------------------------
# Step 2 — noisy-OR maths
# ---------------------------------------------------------------------------


def test_noisy_or_two_fired_verdicts_exact_formula():
    v1 = make_verdict("supervised", fired=True, calibrated_score=0.91, reliability=0.90)
    v2 = make_verdict("beaconing", fired=True, calibrated_score=0.60, reliability=0.50)
    decision = ENGINE.fuse([v1, v2])

    p1 = 0.91 * 0.90
    p2 = 0.60 * 0.50
    expected = 1 - (1 - p1) * (1 - p2)

    assert decision.threat_score == pytest.approx(expected)


def test_noisy_or_single_fired_verdict_equals_its_own_p():
    v = make_verdict("signature", fired=True, calibrated_score=0.4, reliability=0.85)
    decision = ENGINE.fuse([v])
    assert decision.threat_score == pytest.approx(0.4 * 0.85)


def test_monotonicity_adding_a_fired_verdict_never_decreases_score():
    base = [make_verdict("supervised", fired=True, calibrated_score=0.5, reliability=0.9)]
    extra = base + [make_verdict("signature", fired=True, calibrated_score=0.3, reliability=0.85)]

    score_base = ENGINE.fuse(base).threat_score
    score_extra = ENGINE.fuse(extra).threat_score

    assert score_extra >= score_base


def test_monotonicity_holds_even_for_a_weak_added_verdict():
    base = [make_verdict("supervised", fired=True, calibrated_score=0.5, reliability=0.9)]
    extra = base + [weak_volumetric(fired=True)]

    score_base = ENGINE.fuse(base).threat_score
    score_extra = ENGINE.fuse(extra).threat_score

    assert score_extra >= score_base


def test_nonfired_verdicts_contribute_nothing():
    fired_only = [make_verdict("supervised", fired=True, calibrated_score=0.7, reliability=0.9)]
    fired_plus_quiet = fired_only + [
        make_verdict("signature", fired=False, calibrated_score=0.99, reliability=0.85),
        weak_volumetric(fired=False),
    ]

    score_fired_only = ENGINE.fuse(fired_only).threat_score
    score_with_quiet = ENGINE.fuse(fired_plus_quiet).threat_score

    assert score_fired_only == pytest.approx(score_with_quiet)


def test_volumetric_channel_alone_cannot_reach_likely_band():
    """Honest-weighting property: even a maximally-confident volumetric
    verdict (calibrated_score 1.0) is capped by its 0.02 reliability and
    cannot cross into LIKELY."""
    decision = ENGINE.fuse([weak_volumetric(fired=True, score=1.0)])
    assert decision.threat_score == pytest.approx(0.02)
    assert decision.band in (ThreatBand.BENIGN, ThreatBand.SUSPICIOUS)
    assert decision.action is ResponseAction.OBSERVE


# ---------------------------------------------------------------------------
# Step 3 — band boundaries (>= semantics)
# ---------------------------------------------------------------------------


def _engine_with_thresholds() -> HybridFusionEngine:
    return HybridFusionEngine(band_suspicious=0.25, band_likely=0.55, band_confirmed=0.85)


def _single_verdict_with_score(score: float) -> DetectorVerdict:
    # reliability=1.0 so calibrated_score IS the fused score for a single
    # fired verdict (p = score * 1.0, noisy-OR of one term = p).
    return make_verdict("supervised", fired=True, calibrated_score=score, reliability=1.0)


@pytest.mark.parametrize(
    "score,expected_band",
    [
        (0.0, ThreatBand.BENIGN),
        (0.2499, ThreatBand.BENIGN),
        (0.25, ThreatBand.SUSPICIOUS),
        (0.5499, ThreatBand.SUSPICIOUS),
        (0.55, ThreatBand.LIKELY),
        (0.8499, ThreatBand.LIKELY),
        (0.85, ThreatBand.CONFIRMED),
        (1.0, ThreatBand.CONFIRMED),
    ],
)
def test_band_boundaries_use_gte_semantics(score, expected_band):
    engine = _engine_with_thresholds()
    decision = engine.fuse([_single_verdict_with_score(score)])
    assert decision.band is expected_band


def test_action_is_alert_at_likely_and_above_else_observe():
    engine = _engine_with_thresholds()
    below_likely = engine.fuse([_single_verdict_with_score(0.54)])
    at_likely = engine.fuse([_single_verdict_with_score(0.55)])
    confirmed = engine.fuse([_single_verdict_with_score(1.0)])

    assert below_likely.action is ResponseAction.OBSERVE
    assert at_likely.action is ResponseAction.ALERT
    assert confirmed.action is ResponseAction.ALERT


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_verdict_list():
    decision = ENGINE.fuse([])
    assert decision.threat_score == 0.0
    assert decision.band is ThreatBand.BENIGN
    assert decision.action is ResponseAction.OBSERVE
    assert "no detectors" in decision.rationale.lower()
    assert decision.verdicts == ()


def test_all_quiet_verdict_list():
    verdicts = [
        make_verdict("supervised", fired=False, calibrated_score=0.1, reliability=0.9),
        make_verdict("signature", fired=False, calibrated_score=0.05, reliability=0.85),
    ]
    decision = ENGINE.fuse(verdicts)
    assert decision.threat_score == 0.0
    assert decision.band is ThreatBand.BENIGN
    assert decision.action is ResponseAction.OBSERVE
    # Rationale should reflect detectors ran and stayed quiet, distinct
    # from the "no detectors reported" empty-list case.
    assert "no detectors" not in decision.rationale.lower()
    assert decision.verdicts == tuple(verdicts)


# ---------------------------------------------------------------------------
# fuse_batch
# ---------------------------------------------------------------------------


def test_fuse_batch_returns_one_decision_per_flow_in_order():
    flow1 = [confirmed_verdict()]
    flow2 = [make_verdict("supervised", fired=True, calibrated_score=0.9, reliability=0.9)]
    flow3: list[DetectorVerdict] = []

    decisions = ENGINE.fuse_batch([flow1, flow2, flow3])

    assert len(decisions) == 3
    assert decisions[0].band is ThreatBand.CONFIRMED
    assert decisions[1].threat_score == pytest.approx(0.9 * 0.9)
    assert decisions[2].threat_score == 0.0

    # Same as calling fuse() on each individually.
    assert decisions[0] == ENGINE.fuse(flow1)
    assert decisions[1] == ENGINE.fuse(flow2)
    assert decisions[2] == ENGINE.fuse(flow3)


# ---------------------------------------------------------------------------
# FusedDecision.has_confirmed_signal / .fired_detectors
# ---------------------------------------------------------------------------


def test_has_confirmed_signal_true_only_when_a_confirmed_verdict_fired():
    with_confirmed = ENGINE.fuse([confirmed_verdict()])
    without_confirmed = ENGINE.fuse(
        [make_verdict("supervised", fired=True, calibrated_score=0.9, reliability=0.9)]
    )
    confirmed_but_quiet = ENGINE.fuse([confirmed_verdict(fired=False)])

    assert with_confirmed.has_confirmed_signal is True
    assert without_confirmed.has_confirmed_signal is False
    assert confirmed_but_quiet.has_confirmed_signal is False


def test_fired_detectors_lists_only_fired_names_in_verdict_order():
    verdicts = [
        make_verdict("signature", fired=True, calibrated_score=0.4, reliability=0.85),
        make_verdict("beaconing", fired=False, calibrated_score=0.1, reliability=0.5),
        make_verdict("supervised", fired=True, calibrated_score=0.8, reliability=0.9),
    ]
    decision = ENGINE.fuse(verdicts)
    assert decision.fired_detectors == ("signature", "supervised")


# ---------------------------------------------------------------------------
# Never emits THROTTLE / BLOCK
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verdicts",
    [
        [],
        [make_verdict("supervised", fired=False, calibrated_score=0.0, reliability=0.9)],
        [weak_volumetric(fired=True, score=1.0)],
        [make_verdict("supervised", fired=True, calibrated_score=1.0, reliability=1.0)],
        [confirmed_verdict()],
        [
            confirmed_verdict(),
            weak_volumetric(fired=True, score=1.0),
            make_verdict("beaconing", fired=True, calibrated_score=1.0, reliability=1.0),
        ],
        [
            make_verdict("supervised", fired=True, calibrated_score=1.0, reliability=1.0),
            make_verdict("signature", fired=True, calibrated_score=1.0, reliability=1.0),
            make_verdict("beaconing", fired=True, calibrated_score=1.0, reliability=1.0),
        ],
    ],
)
def test_never_emits_throttle_or_block(verdicts):
    decision = ENGINE.fuse(verdicts)
    assert decision.action in (ResponseAction.OBSERVE, ResponseAction.ALERT)
    assert decision.action is not ResponseAction.THROTTLE
    assert decision.action is not ResponseAction.BLOCK


# ---------------------------------------------------------------------------
# Constructor / band_thresholds introspection
# ---------------------------------------------------------------------------


def test_default_constructor_reads_backend_settings():
    from backend.config import BACKEND_SETTINGS

    engine = HybridFusionEngine()
    assert engine.band_thresholds == {
        "suspicious": BACKEND_SETTINGS.hybrid_band_suspicious,
        "likely": BACKEND_SETTINGS.hybrid_band_likely,
        "confirmed": BACKEND_SETTINGS.hybrid_band_confirmed,
    }


def test_constructor_override_does_not_mutate_backend_settings():
    from backend.config import BACKEND_SETTINGS

    original = BackendSettings()
    engine = HybridFusionEngine(band_suspicious=0.1, band_likely=0.2, band_confirmed=0.3)

    assert engine.band_thresholds == {"suspicious": 0.1, "likely": 0.2, "confirmed": 0.3}
    assert BACKEND_SETTINGS.hybrid_band_suspicious == original.hybrid_band_suspicious
    assert BACKEND_SETTINGS.hybrid_band_likely == original.hybrid_band_likely
    assert BACKEND_SETTINGS.hybrid_band_confirmed == original.hybrid_band_confirmed


def test_band_thresholds_override_changes_banding():
    engine = HybridFusionEngine(band_suspicious=0.01, band_likely=0.02, band_confirmed=0.03)
    decision = engine.fuse([weak_volumetric(fired=True, score=1.0)])  # score 0.02
    assert decision.band is ThreatBand.LIKELY
    assert decision.action is ResponseAction.ALERT


# ---------------------------------------------------------------------------
# Sanity: verdicts tuple is preserved on the decision
# ---------------------------------------------------------------------------


def test_verdicts_preserved_on_decision():
    verdicts = [
        make_verdict("supervised", fired=True, calibrated_score=0.9, reliability=0.9),
        weak_volumetric(fired=False),
    ]
    decision = ENGINE.fuse(verdicts)
    assert isinstance(decision, FusedDecision)
    assert decision.verdicts == tuple(verdicts)
