"""
metrics.py — Segment-wise precision/recall for time-series ICS anomaly
detection (AEGIS Phase 3).

Why not point-adjust
---------------------
"Point-adjust" is a widely-used post-hoc scoring convention in time-series
anomaly-detection benchmarks: once a detector fires on ANY single row inside
a ground-truth anomaly segment, the ENTIRE segment is retroactively treated
as "detected" for both precision and recall. `research/BENCHMARKS.md`
(finding B5, citing the ESORICS 2022 ICS anomaly-detection evaluation suite)
is blunt about the consequence: point-adjust "can inflate scores by masking
timing errors," to the point that "a detection algorithm outputting random
noise is expected to produce very good scores, and capable of outperforming
state of the art methods." The mechanism is straightforward: ICS attack
segments (e.g. SWaT) run for minutes, so a detector need only get lucky ONCE
per segment — even on the very last row — to have the *whole* segment
counted as a hit, which is information the detector never actually had at
any single point in time it was scored.

What segment-wise scoring does instead
---------------------------------------
1. Ground truth is grouped into contiguous "segments": maximal runs of
   consecutive rows where ``y_true == 1`` (an attack window), bounded by
   ``y_true == 0`` rows or the ends of the series (see `find_segments`).
2. RECALL is scored per segment, not per row: a segment counts as "found"
   if the detector fired on AT LEAST ONE row within it. This is the one
   piece of point-adjust's intuition this module keeps, because it matches
   how a human operator actually experiences an attack window — one alert
   inside a 10-minute attack is a real detection, not a near-miss — and a
   single long attack should not be scored as though it were many
   independent rows the detector had to catch every one of.
3. PRECISION is scored per ROW, not per segment. This is the deliberate,
   load-bearing split from point-adjust. Point-adjust's actual mistake is
   inflating *precision*: once a segment is marked "detected," every row
   inside it — including rows the detector never flagged — gets counted as
   a true positive when tallying precision, which launders false negatives
   into true positives and makes a noisy, mistimed detector look clean. No
   such amnesty is applied here: a row the detector did not flag is never
   counted as a hit for precision purposes, and a false positive on a
   benign row always counts against precision exactly as it would under
   ordinary point-wise scoring.

Net effect versus naive point-wise scoring on the same predictions: recall
is more forgiving (one alert saves an entire long attack window instead of
requiring every row inside it to be flagged), while precision is exactly as
strict as point-wise scoring — no segment gets a free pass. That asymmetry
is what the point-adjust critique recommends, and it is the one thing plain
point-adjust does not have (point-adjust inflates *both* metrics using
future information).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


def find_segments(y_true: np.ndarray) -> list[tuple[int, int]]:
    """Group contiguous ``y_true == 1`` runs into ``(start, end)`` index pairs.

    Parameters
    ----------
    y_true:
        1D array-like of binary ground-truth labels (1 = anomalous row),
        in the original time order of the series.

    Returns
    -------
    list[tuple[int, int]]
        Inclusive ``(start_index, end_index)`` pairs, one per contiguous
        anomaly segment, in the order they occur in ``y_true``.
    """
    y = np.asarray(y_true).astype(int)
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for i, v in enumerate(y):
        if v == 1 and start is None:
            start = i
        elif v == 0 and start is not None:
            segments.append((start, i - 1))
            start = None
    if start is not None:
        segments.append((start, len(y) - 1))
    return segments


@dataclass
class SegmentWiseMetrics:
    """Segment-wise recall + row-wise precision, and the counts behind them."""

    precision: float
    recall: float
    f1: float
    n_segments: int
    n_segments_detected: int
    n_true_anomalous_rows: int
    n_predicted_anomalous_rows: int
    n_true_positive_rows: int

    def to_dict(self) -> dict:
        return asdict(self)


def segment_wise_precision_recall(
    y_true: np.ndarray,
    y_pred: np.ndarray,
) -> SegmentWiseMetrics:
    """Compute segment-wise recall + row-wise precision for a time series.

    See the module docstring for why recall is segment-wise while precision
    stays row-wise (the deliberate rejection of point-adjust).

    Parameters
    ----------
    y_true:
        1D binary ground-truth labels, in original time order (required —
        contiguity is meaningless on a shuffled series).
    y_pred:
        1D binary detector predictions (1 = flagged anomalous), same order
        and length as ``y_true``.

    Returns
    -------
    SegmentWiseMetrics
    """
    y_true = np.asarray(y_true).astype(int)
    y_pred = np.asarray(y_pred).astype(int)
    if y_true.shape != y_pred.shape:
        raise ValueError(
            f"y_true and y_pred must have the same shape, got {y_true.shape} "
            f"and {y_pred.shape}"
        )

    segments = find_segments(y_true)
    n_detected = 0
    for start, end in segments:
        if y_pred[start : end + 1].any():
            n_detected += 1
    recall = (n_detected / len(segments)) if segments else 0.0

    # Row-wise precision — deliberately NOT segment-amnestied. See docstring.
    tp_rows = int(np.sum((y_pred == 1) & (y_true == 1)))
    fp_rows = int(np.sum((y_pred == 1) & (y_true == 0)))
    precision = (tp_rows / (tp_rows + fp_rows)) if (tp_rows + fp_rows) > 0 else 0.0

    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return SegmentWiseMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        n_segments=len(segments),
        n_segments_detected=n_detected,
        n_true_anomalous_rows=int(y_true.sum()),
        n_predicted_anomalous_rows=int(y_pred.sum()),
        n_true_positive_rows=tp_rows,
    )
