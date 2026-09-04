"""
tests/test_ingest_hybrid.py — Hybrid IDS integration: `IngestPipeline`
wiring to `backend/detection/` (signature, beaconing, fusion).

Reuses `tests/test_ingest.py`'s fakes (`FakeScorer`, `FakeSession`,
`make_flow`, `make_pipeline`, `make_meta`, `rows_of`, `stmts_for`) rather
than duplicating that plumbing — same cross-file reuse pattern
`tests/test_security.py` uses against `tests/test_api.py`.

Scope: this file tests the WIRING (does `IngestPipeline` correctly
persist/alert/broadcast a fused decision), not the detectors' own
algorithms — those are tests/test_signature.py, tests/test_beaconing.py,
tests/test_fusion.py.
"""

from __future__ import annotations

from typing import Sequence

from backend.detection.beaconing import BeaconingDetector
from backend.detection.contracts import (
    DETECTOR_BEACONING,
    DETECTOR_HYBRID,
    DETECTOR_SIGNATURE,
    DETECTOR_TGNN,
    Certainty,
    DetectorVerdict,
    FlowFeatures,
)
from backend.detection.fusion import HybridFusionEngine
from backend.detection.signature import SignatureEngine
from backend.ingest import (
    DETECTOR_VOLUMETRIC,
    ENVELOPE_CII,
    ENVELOPE_EVENT,
    SEVERITY_WARNING,
    TITLE_HYBRID,
    TITLE_TRIPWIRE,
    IngestPipeline,
)
from tests.test_ingest import (
    FakeScorer,
    make_flow,
    make_meta,
    make_pipeline,
    rows_of,
    stmts_for,
)


# ---------------------------------------------------------------------------
# Stub detectors — deterministic, no reliance on the real algorithms'
# thresholds (those are exercised by their own dedicated test files).
# ---------------------------------------------------------------------------


class _AlwaysFireDetector:
    """A `FlowDetector` that fires on every flow at a fixed confidence.

    `certainty=Certainty.HEURISTIC` by default, deliberately: these tests
    exercise the noisy-OR band-crossing path, not the CONFIRMED-signal
    precedence path (that path is already pinned by
    tests/test_fusion.py's dilution-guard test).
    """

    def __init__(self, name: str, calibrated_score: float = 0.9, reliability: float = 1.0):
        self.name = name
        self._score = calibrated_score
        self._reliability = reliability

    def examine(self, flows: Sequence[FlowFeatures]) -> list[DetectorVerdict]:
        return [
            DetectorVerdict(
                detector=self.name,
                fired=True,
                calibrated_score=self._score,
                reliability=self._reliability,
                certainty=Certainty.HEURISTIC,
                evidence={"stub": "always_fire"},
            )
            for _ in flows
        ]


class _NeverFireDetector:
    def __init__(self, name: str):
        self.name = name

    def examine(self, flows: Sequence[FlowFeatures]) -> list[DetectorVerdict]:
        return [
            DetectorVerdict(
                detector=self.name,
                fired=False,
                calibrated_score=0.0,
                reliability=0.5,
                evidence={"stub": "never_fire"},
            )
            for _ in flows
        ]


class _FakeClock:
    """Injectable clock for deterministic debounce tests — mirrors the
    `clock` param `IngestPipeline` already accepts for exactly this
    purpose."""

    def __init__(self, start: float = 0.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _escalating_pipeline(**kw):
    """A pipeline whose EXISTING channels stay quiet (no tripwire, no
    volumetric fire) but whose signature stub fires hard enough to clear
    the alert band on its own — the scenario `_hybrid_alert_decision`
    exists for."""
    return make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: False,
        signature_engine=_AlwaysFireDetector(DETECTOR_SIGNATURE),
        beaconing_detector=_NeverFireDetector(DETECTOR_BEACONING),
        **kw,
    )


# ---------------------------------------------------------------------------
# Persistence: the hybrid event_scores row
# ---------------------------------------------------------------------------


def test_hybrid_row_written_per_event_when_enabled():
    pipeline, session = make_pipeline(scorer=FakeScorer(anomaly_flags=[False]))
    pipeline([make_flow("f:1")], make_meta())
    rows = rows_of(stmts_for(session, "event_scores")[0])
    hybrid_rows = [r for r in rows if r["detector"] == DETECTOR_HYBRID]
    assert len(hybrid_rows) == 1
    # calibrated_score and confidence deliberately carry the SAME fused
    # figure (see _persist_scores's docstring) — not the fused decision's
    # own raw_score, which is None (no single native unit).
    assert hybrid_rows[0]["raw_score"] is None
    assert hybrid_rows[0]["calibrated_score"] == hybrid_rows[0]["confidence"]


def test_no_hybrid_row_when_hybrid_disabled():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]), hybrid_enabled=False
    )
    pipeline([make_flow("f:1")], make_meta())
    rows = rows_of(stmts_for(session, "event_scores")[0])
    assert DETECTOR_HYBRID not in {r["detector"] for r in rows}


def test_hybrid_row_survives_deduplication_too():
    """Companion to test_ingest.py::test_deduplicated_rows_get_no_duplicate_scores
    (which disables hybrid to isolate the dedup invariant) — this is the
    hybrid-enabled equivalent, proving dedup applies uniformly to every
    detector's row, not just the pre-existing ones."""
    from tests.test_ingest import FakeSession

    session = FakeSession(next_event_ids=[1])  # only the first row inserted
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False, False]), session=session
    )
    result = pipeline([make_flow("f:1"), make_flow("f:2")], make_meta())
    assert result.events_inserted == 1
    assert result.events_deduplicated == 1
    rows = rows_of(stmts_for(session, "event_scores")[0])
    # volumetric + hybrid + the three always-on heuristic channels
    # (signature/beaconing/tgnn abstain on this tiny batch but still get
    # a row each — see _persist_scores's docstring) for the ONE real
    # event, nothing for the deduped one.
    assert len(rows) == 5
    assert {r["detector"] for r in rows} == {
        DETECTOR_VOLUMETRIC,
        DETECTOR_HYBRID,
        DETECTOR_SIGNATURE,
        DETECTOR_BEACONING,
        DETECTOR_TGNN,
    }


# ---------------------------------------------------------------------------
# Alerting: hybrid_gates_alerts (default False), the widened gate, and
# the double-alert guard
# ---------------------------------------------------------------------------


def test_hybrid_gates_alerts_off_by_default_creates_no_alert():
    pipeline, session = _escalating_pipeline()  # hybrid_gates_alerts left at default (False)
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 0
    assert session.alerts() == []
    assert result.hybrid_gated_alerts == 0


def test_hybrid_gates_alerts_on_creates_alert_when_existing_channels_are_quiet():
    pipeline, session = _escalating_pipeline(hybrid_gates_alerts=True)
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 1
    assert result.hybrid_gated_alerts == 1
    [alert] = session.alerts()
    assert alert.title == TITLE_HYBRID
    assert alert.severity == SEVERITY_WARNING
    assert DETECTOR_SIGNATURE in alert.explanation["rationale"] or any(
        v["detector"] == DETECTOR_SIGNATURE and v["fired"] for v in alert.explanation["verdicts"]
    )


def test_hybrid_never_double_alerts_when_tripwire_already_fired():
    """A CONFIRMED tripwire signal already alerts through the existing
    path (already_anomaly=True) — the hybrid layer must not ALSO create
    a second alert for the same flow even with hybrid_gates_alerts=True
    and a firing signature stub."""
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: True,
        signature_engine=_AlwaysFireDetector(DETECTOR_SIGNATURE),
        beaconing_detector=_NeverFireDetector(DETECTOR_BEACONING),
        hybrid_gates_alerts=True,
    )
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 1
    assert result.hybrid_gated_alerts == 0  # the ONE alert came from tripwire, not hybrid
    [alert] = session.alerts()
    assert alert.title == TITLE_TRIPWIRE


def test_hybrid_gated_alert_respects_shared_asset_debounce():
    """The hybrid path must share `_last_alert_at` with the existing
    tripwire/volumetric path (see `_debounce_ok`'s docstring) — two
    escalation-eligible batches on the SAME asset inside the debounce
    window produce exactly one alert, not two."""
    clock = _FakeClock(start=0.0)
    pipeline, session = _escalating_pipeline(
        hybrid_gates_alerts=True,
        alert_asset_debounce_sec=60.0,
        clock=clock,
    )
    r1 = pipeline([make_flow("f:1")], make_meta(batch_index=1))
    clock.advance(5.0)  # well inside the 60s debounce window
    r2 = pipeline([make_flow("f:2")], make_meta(batch_index=2))
    assert r1.alerts_created == 1
    assert r2.alerts_created == 0  # debounced
    assert len(session.alerts()) == 1


def test_hybrid_candidate_precheck_skips_cii_for_an_ordinary_quiet_flow():
    """Performance guard (P5-17): an ordinary flow that no detector
    (real SignatureEngine/BeaconingDetector, no stubs here) flags must
    not trigger a wasted Monte Carlo CII computation just because the
    hybrid layer is enabled and observing every flow."""
    pipeline, session = make_pipeline(scorer=FakeScorer(anomaly_flags=[False]))
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.cii_computed == 0
    assert session.snapshots() == []


# ---------------------------------------------------------------------------
# Broadcast envelope: additive `hybrid` key
# ---------------------------------------------------------------------------


def test_hybrid_only_alert_gates_the_cii_broadcast_too():
    """UNLIKE the existing tripwire/volumetric channel (see
    tests/test_ingest.py::
    test_cii_envelope_still_broadcast_when_alert_is_debounce_suppressed,
    which broadcasts cii regardless of alert suppression), a
    hybrid-candidate flow with hybrid_gates_alerts left at its default
    (False) must NOT broadcast a cii envelope either -- there is no
    existing-channel signal behind it, only the hybrid layer's own
    fused opinion, which is explicitly observable-not-authoritative
    while this setting is off. Lighting up the graph's cascade overlay
    with nothing in the alerts panel to explain it would contradict
    that posture."""
    from backend.ingest import CollectingBroadcaster

    broadcaster = CollectingBroadcaster()
    pipeline, session = _escalating_pipeline(broadcaster=broadcaster)  # hybrid_gates_alerts default False
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 0
    assert broadcaster.of_type(ENVELOPE_CII) == []


def test_hybrid_gated_alert_does_broadcast_its_cii_envelope():
    """Companion to the test above: once hybrid_gates_alerts=True and the
    hybrid layer's own decision actually creates an alert, its cii
    envelope IS broadcast -- there is now a real alert in the panel to
    account for the cascade shown."""
    from backend.ingest import CollectingBroadcaster

    broadcaster = CollectingBroadcaster()
    pipeline, session = _escalating_pipeline(hybrid_gates_alerts=True, broadcaster=broadcaster)
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 1
    assert result.hybrid_gated_alerts == 1
    cii_envelopes = broadcaster.of_type(ENVELOPE_CII)
    assert len(cii_envelopes) == 1
    assert cii_envelopes[0]["data"]["origin_asset"] == "City_Payment_Gateway"


def test_broadcast_envelope_carries_hybrid_summary_when_enabled():
    from backend.ingest import CollectingBroadcaster

    broadcaster = CollectingBroadcaster()
    pipeline, session = _escalating_pipeline(broadcaster=broadcaster)
    pipeline([make_flow("f:1")], make_meta())
    [envelope] = broadcaster.of_type(ENVELOPE_EVENT)
    hybrid = envelope["data"]["hybrid"]
    assert hybrid is not None
    assert set(hybrid.keys()) == {"threat_score", "band", "action", "fired_detectors", "rationale"}
    assert DETECTOR_SIGNATURE in hybrid["fired_detectors"]
    assert hybrid["action"] == "alert"


def test_broadcast_envelope_hybrid_is_none_when_disabled():
    from backend.ingest import CollectingBroadcaster

    broadcaster = CollectingBroadcaster()
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]), hybrid_enabled=False, broadcaster=broadcaster
    )
    pipeline([make_flow("f:1")], make_meta())
    [envelope] = broadcaster.of_type(ENVELOPE_EVENT)
    assert envelope["data"]["hybrid"] is None


# ---------------------------------------------------------------------------
# Telemetry rollup
# ---------------------------------------------------------------------------


def test_batch_result_hybrid_telemetry_counts_detector_hits():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False, False]),
        tripwire_signal=lambda f: False,
        signature_engine=_AlwaysFireDetector(DETECTOR_SIGNATURE),
        beaconing_detector=_NeverFireDetector(DETECTOR_BEACONING),
    )
    result = pipeline([make_flow("f:1"), make_flow("f:2")], make_meta())
    assert result.hybrid_signature_hits == 2
    assert result.hybrid_beaconing_hits == 0
    assert result.hybrid_likely_or_above == 2


# ---------------------------------------------------------------------------
# Default construction wires the REAL detectors, not just the injectable
# stubs the tests above use
# ---------------------------------------------------------------------------


def test_default_construction_builds_real_detection_layer():
    pipeline = IngestPipeline(scorer=FakeScorer())
    assert isinstance(pipeline._signature_engine, SignatureEngine)
    assert isinstance(pipeline._beaconing_detector, BeaconingDetector)
    assert isinstance(pipeline._fusion_engine, HybridFusionEngine)


def test_beaconing_detector_instance_persists_across_batches():
    """BeaconingDetector is STATEFUL and must be held as one long-lived
    instance on the pipeline, never rebuilt per batch — rebuilding it
    would silently reset every pair's inter-arrival history every batch
    and the detector could never leave its abstain state (see the
    constructor's docstring in backend/ingest.py)."""
    detector = BeaconingDetector()
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False, False]),
        beaconing_detector=detector,
    )
    assert detector.tracked_pairs == 0
    pipeline([make_flow("f:1")], make_meta(batch_index=1))
    pipeline([make_flow("f:2")], make_meta(batch_index=2))
    # Same source_ip/destination_ip on both calls (make_flow's defaults) —
    # if the detector were rebuilt per batch this would still read 0 or 1
    # depending on rebuild timing, never reliably >= 1 with real history.
    assert pipeline._beaconing_detector is detector
    assert detector.tracked_pairs >= 1
