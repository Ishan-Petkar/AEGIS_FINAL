"""
tests/test_beaconing.py — backend/detection/beaconing.py.

Plain pytest functions (mirrors tests/test_security.py's style), no
fixtures beyond a small local flow-builder helper. Flows are constructed
directly as `FlowFeatures` — the contract this detector consumes — rather
than through `ReplayFlow`/`from_replay_flow`, since none of the excluded
ground-truth fields matter here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.detection.beaconing import BeaconingDetector
from backend.detection.contracts import DETECTOR_BEACONING, Certainty, FlowFeatures


def _flow(ts: datetime, src: str = "10.0.0.1", dst: str = "10.0.0.2") -> FlowFeatures:
    return FlowFeatures(
        ts=ts,
        source_ip=src,
        source_port=54321,
        destination_ip=dst,
        destination_port=443,
        protocol="TCP",
        duration_sec=0.01,
        packets=2,
        bytes=6,
    )


_T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _periodic_flows(n: int, period_sec: float, src="10.0.0.1", dst="10.0.0.2"):
    return [_flow(_T0 + timedelta(seconds=i * period_sec), src, dst) for i in range(n)]


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


def test_perfectly_periodic_pair_fires_with_high_score():
    detector = BeaconingDetector()
    flows = _periodic_flows(n=10, period_sec=30.0)
    verdicts = detector.examine(flows)

    last = verdicts[-1]
    assert last.fired is True
    assert last.calibrated_score > 0.9
    assert last.detector == DETECTOR_BEACONING
    assert last.certainty is Certainty.HEURISTIC
    assert last.evidence["cv"] == pytest.approx(0.0, abs=1e-9)


def test_bursty_irregular_pair_does_not_fire():
    detector = BeaconingDetector()
    # Wildly irregular gaps, well above the interval floor so they're all
    # usable, but nowhere near periodic.
    offsets = [0, 2, 3, 40, 41, 200, 205, 900, 901, 902]
    flows = [_flow(_T0 + timedelta(seconds=o)) for o in offsets]
    verdicts = detector.examine(flows)

    last = verdicts[-1]
    assert last.fired is False
    assert last.calibrated_score < 0.5


def test_abstains_below_min_samples_and_evidence_says_why():
    detector = BeaconingDetector(min_samples=5)
    # Only 2 flows -> 1 interval, well short of required 4.
    flows = _periodic_flows(n=2, period_sec=30.0)
    verdicts = detector.examine(flows)

    for v in verdicts:
        assert v.fired is False
        assert v.calibrated_score == 0.0
        assert v.evidence["abstained"] == "insufficient_history"

    assert verdicts[-1].evidence["usable_intervals"] == 1
    assert verdicts[-1].evidence["required_intervals"] == 4


# ---------------------------------------------------------------------------
# Contract: one verdict per input flow, in order
# ---------------------------------------------------------------------------


def test_one_verdict_per_flow_in_order_for_mixed_batch():
    detector = BeaconingDetector()
    flows = [
        _flow(_T0, src="10.0.0.1", dst="10.0.0.2"),
        _flow(_T0 + timedelta(seconds=1), src="10.0.0.3", dst="10.0.0.4"),
        _flow(_T0 + timedelta(seconds=30), src="10.0.0.1", dst="10.0.0.2"),
        _flow(_T0 + timedelta(seconds=2), src="10.0.0.5", dst="10.0.0.6"),
        _flow(_T0 + timedelta(seconds=60), src="10.0.0.1", dst="10.0.0.2"),
    ]
    verdicts = detector.examine(flows)

    assert len(verdicts) == len(flows)
    assert all(v.detector == DETECTOR_BEACONING for v in verdicts)
    # Distinct singleton pairs (indices 1, 3) must both abstain — each has
    # zero intervals of its own.
    assert verdicts[1].evidence["abstained"] == "insufficient_history"
    assert verdicts[3].evidence["abstained"] == "insufficient_history"


def test_empty_batch_returns_empty_list():
    detector = BeaconingDetector()
    assert detector.examine([]) == []


# ---------------------------------------------------------------------------
# Interval floor: shared-timestamp bursts must not manufacture a beacon
# ---------------------------------------------------------------------------


def test_interval_floor_excludes_same_timestamp_burst():
    detector = BeaconingDetector(min_interval_sec=0.5, min_samples=3)
    # All flows share exactly the same second (minute-granularity-style
    # burst) -> every raw interval is 0.0, below the 0.5s floor.
    flows = [_flow(_T0) for _ in range(6)]
    verdicts = detector.examine(flows)

    last = verdicts[-1]
    assert last.fired is False
    assert last.evidence["abstained"] == "insufficient_history"
    assert last.evidence["usable_intervals"] == 0
    assert last.evidence["total_intervals_observed"] == 5


def test_interval_ceiling_excludes_long_gaps():
    detector = BeaconingDetector(max_interval_sec=100.0, min_samples=3)
    # Two clusters of tight, regular intervals separated by one huge gap.
    offsets = [0, 10, 20, 5000, 5010, 5020]
    flows = [_flow(_T0 + timedelta(seconds=o)) for o in offsets]
    verdicts = detector.examine(flows)

    last = verdicts[-1]
    # The 4980s gap is excluded; the remaining 10s-spaced intervals are
    # regular, so this should still evaluate (not silently abstain on a
    # gap that was correctly filtered out).
    assert last.evidence["total_intervals_observed"] == 5
    assert last.evidence["usable_intervals"] == 4
    assert last.fired is True


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


def test_lru_eviction_caps_tracked_pairs():
    detector = BeaconingDetector(max_tracked_pairs=3)
    flows = [_flow(_T0, src=f"10.0.0.{i}", dst="10.0.0.100") for i in range(10)]
    detector.examine(flows)

    assert detector.tracked_pairs == 3


def test_lru_eviction_drops_least_recently_used_pair():
    detector = BeaconingDetector(max_tracked_pairs=2, min_samples=3)
    # Pair A gets touched first and again right before the cap is
    # breached, so it should survive; pair B (touched once, long ago)
    # should be the one evicted.
    flows = [
        _flow(_T0, src="A", dst="Z"),
        _flow(_T0, src="B", dst="Z"),
        _flow(_T0 + timedelta(seconds=1), src="A", dst="Z"),
        _flow(_T0, src="C", dst="Z"),  # breaches cap of 2 -> evicts B
    ]
    detector.examine(flows)

    assert detector.tracked_pairs == 2
    assert ("A", "Z") in detector._history
    assert ("C", "Z") in detector._history
    assert ("B", "Z") not in detector._history


# ---------------------------------------------------------------------------
# reset() and determinism
# ---------------------------------------------------------------------------


def test_reset_clears_state():
    detector = BeaconingDetector()
    detector.examine(_periodic_flows(n=5, period_sec=10.0))
    assert detector.tracked_pairs > 0

    detector.reset()
    assert detector.tracked_pairs == 0


def test_deterministic_same_input_twice_after_reset():
    flows = _periodic_flows(n=8, period_sec=15.0) + [
        _flow(_T0 + timedelta(seconds=500), src="10.0.0.9", dst="10.0.0.10"),
    ]

    detector = BeaconingDetector()
    first = detector.examine(flows)

    detector.reset()
    second = detector.examine(flows)

    assert len(first) == len(second)
    for v1, v2 in zip(first, second):
        assert v1.fired == v2.fired
        assert v1.calibrated_score == v2.calibrated_score
        assert v1.raw_score == v2.raw_score
        assert v1.evidence == v2.evidence


# ---------------------------------------------------------------------------
# calibrated_score always within contract bounds
# ---------------------------------------------------------------------------


def test_calibrated_score_always_in_unit_interval():
    detector = BeaconingDetector()
    offsets = [0, 1, 1, 2, 500, 501, 900, 5000, 5001, 5002, 5003]
    flows = [_flow(_T0 + timedelta(seconds=o)) for o in offsets]
    verdicts = detector.examine(flows)
    for v in verdicts:
        assert 0.0 <= v.calibrated_score <= 1.0
