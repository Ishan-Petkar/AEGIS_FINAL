"""
test_evaluation_metrics.py — Tests for segment-wise precision/recall
(AEGIS Phase 3, evaluation/metrics.py).

Pins the specific methodology the module docstring documents: recall is
segment-wise (one hit anywhere in a contiguous ground-truth segment is
enough), while precision stays row-wise (no segment gets amnesty) — the
deliberate rejection of "point-adjust" scoring.
"""
import pathlib
import sys

import numpy as np
import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from evaluation.metrics import find_segments, segment_wise_precision_recall


# ---------------------------------------------------------------------------
# find_segments
# ---------------------------------------------------------------------------

class TestFindSegments:
    def test_no_anomalies(self):
        assert find_segments(np.array([0, 0, 0, 0])) == []

    def test_all_anomalies(self):
        assert find_segments(np.array([1, 1, 1])) == [(0, 2)]

    def test_single_segment_in_middle(self):
        y = np.array([0, 0, 1, 1, 1, 0, 0])
        assert find_segments(y) == [(2, 4)]

    def test_multiple_segments(self):
        y = np.array([1, 0, 1, 1, 0, 0, 1])
        assert find_segments(y) == [(0, 0), (2, 3), (6, 6)]

    def test_segment_touching_end_of_series(self):
        y = np.array([0, 0, 1, 1])
        assert find_segments(y) == [(2, 3)]

    def test_single_point_segments(self):
        y = np.array([1, 0, 1, 0, 1])
        assert find_segments(y) == [(0, 0), (2, 2), (4, 4)]


# ---------------------------------------------------------------------------
# segment_wise_precision_recall
# ---------------------------------------------------------------------------

class TestSegmentWisePrecisionRecall:
    def test_shape_mismatch_raises(self):
        with pytest.raises(ValueError):
            segment_wise_precision_recall(np.array([1, 0]), np.array([1, 0, 1]))

    def test_perfect_detector(self):
        y_true = np.array([0, 0, 1, 1, 1, 0, 0])
        y_pred = np.array([0, 0, 1, 1, 1, 0, 0])
        m = segment_wise_precision_recall(y_true, y_pred)
        assert m.recall == pytest.approx(1.0)
        assert m.precision == pytest.approx(1.0)
        assert m.f1 == pytest.approx(1.0)
        assert m.n_segments == 1
        assert m.n_segments_detected == 1

    def test_one_hit_anywhere_in_segment_counts_as_recalled(self):
        """The one piece of point-adjust's intuition segment-wise scoring
        DOES keep: a single alert anywhere inside a long attack segment
        counts as that segment being found, for RECALL purposes."""
        y_true = np.array([0, 1, 1, 1, 1, 1, 0])  # one 5-row segment
        y_pred = np.array([0, 0, 0, 0, 0, 1, 0])  # detector fires on the LAST row only
        m = segment_wise_precision_recall(y_true, y_pred)
        assert m.n_segments == 1
        assert m.n_segments_detected == 1
        assert m.recall == pytest.approx(1.0)

    def test_missed_segment_scores_zero_recall_for_that_segment(self):
        y_true = np.array([1, 1, 0, 1, 1])  # two segments
        y_pred = np.array([1, 1, 0, 0, 0])  # only the first segment is hit
        m = segment_wise_precision_recall(y_true, y_pred)
        assert m.n_segments == 2
        assert m.n_segments_detected == 1
        assert m.recall == pytest.approx(0.5)

    def test_precision_does_not_get_segment_amnesty(self):
        """The deliberate rejection of point-adjust: even though the
        detector found the segment (one hit is enough for recall), rows
        inside the segment it did NOT flag must NOT be silently counted as
        true positives for precision. Point-adjust would inflate precision
        here; segment-wise scoring must not."""
        y_true = np.array([0, 1, 1, 1, 1, 1, 0])  # 5-row segment
        y_pred = np.array([0, 0, 0, 0, 0, 1, 0])  # only the last row flagged
        m = segment_wise_precision_recall(y_true, y_pred)
        # Recall is perfect (segment found)...
        assert m.recall == pytest.approx(1.0)
        # ...but precision is computed row-wise: 1 true positive row, 0 false
        # positives -> precision 1.0 here (no false alarms), NOT because the
        # whole segment was credited, but because every predicted-positive
        # row genuinely was inside the true segment.
        assert m.n_true_positive_rows == 1
        assert m.precision == pytest.approx(1.0)

    def test_false_positive_outside_any_segment_hurts_precision(self):
        y_true = np.array([0, 0, 1, 1, 0, 0])
        y_pred = np.array([0, 1, 1, 0, 0, 0])  # one FP (idx 1), one TP (idx 2)
        m = segment_wise_precision_recall(y_true, y_pred)
        assert m.n_true_positive_rows == 1
        assert m.precision == pytest.approx(0.5)  # 1 TP / (1 TP + 1 FP)

    def test_point_adjust_would_inflate_precision_but_this_does_not(self):
        """Direct contrast: a detector that fires on exactly one row inside
        a long true segment, and nowhere else. Point-adjust would mark the
        ENTIRE segment as 'detected' and then count every row in it as a
        true positive for precision (a 10/10 row precision). Segment-wise
        scoring here must only credit the one row it actually flagged."""
        y_true = np.array([1] * 10)  # one 10-row segment, no benign rows at all
        y_pred = np.array([0] * 9 + [1])  # fires once, on the last row
        m = segment_wise_precision_recall(y_true, y_pred)
        assert m.n_segments_detected == 1  # segment recalled
        assert m.n_true_positive_rows == 1  # NOT 10 — no point-adjust amnesty
        assert m.precision == pytest.approx(1.0)  # 1 TP / (1 TP + 0 FP)

    def test_no_segments_gives_zero_recall_not_undefined(self):
        y_true = np.array([0, 0, 0, 0])
        y_pred = np.array([0, 1, 0, 0])
        m = segment_wise_precision_recall(y_true, y_pred)
        assert m.n_segments == 0
        assert m.recall == 0.0

    def test_no_predictions_gives_zero_precision_not_undefined(self):
        y_true = np.array([0, 1, 1, 0])
        y_pred = np.array([0, 0, 0, 0])
        m = segment_wise_precision_recall(y_true, y_pred)
        assert m.precision == 0.0
        assert m.recall == 0.0

    def test_to_dict_has_required_keys(self):
        y_true = np.array([0, 1, 1, 0])
        y_pred = np.array([0, 1, 0, 0])
        m = segment_wise_precision_recall(y_true, y_pred)
        d = m.to_dict()
        for key in ("precision", "recall", "f1", "n_segments",
                    "n_segments_detected", "n_true_anomalous_rows",
                    "n_predicted_anomalous_rows", "n_true_positive_rows"):
            assert key in d
