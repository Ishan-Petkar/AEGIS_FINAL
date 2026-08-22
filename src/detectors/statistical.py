"""
statistical.py — Robust statistical baseline detectors for AEGIS.

Moved here from ml_engine.py (Phase 1, contract C3) and formalised as BaseDetector
subclasses; ml_engine.py re-exports both names so existing imports keep working.
"""
from __future__ import annotations

import numpy as np

from detectors.base import BaseDetector


class ZScoreDetector(BaseDetector):
    """Univariate Z-Score anomaly detector.

    For each sample, computes the maximum absolute Z-score across all features.
    Flags samples whose max Z-score exceeds ``threshold`` as anomalies.
    """

    def __init__(self, threshold: float = 3.0) -> None:
        self.threshold = threshold
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None

    def fit(self, X: np.ndarray) -> "ZScoreDetector":
        self._mean = X.mean(axis=0)
        self._std = X.std(axis=0)
        self._std = np.where(self._std == 0, 1.0, self._std)  # avoid /0
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Return -1 for anomaly, 1 for normal (matching sklearn convention)."""
        z = np.abs((X - self._mean) / self._std)
        max_z = z.max(axis=1)
        return np.where(max_z > self.threshold, -1, 1)

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        """Return negative max-Z score so that lower = more anomalous (sklearn convention)."""
        z = np.abs((X - self._mean) / self._std)
        return -z.max(axis=1)


class MADDetector(BaseDetector):
    """Median Absolute Deviation (MAD) anomaly detector.

    Robust alternative to Z-Score; uses median and MAD rather than mean/std,
    making it resistant to outliers skewing the reference distribution.
    Flags samples whose max modified-Z exceeds ``threshold``.
    """

    def __init__(self, threshold: float = 3.5) -> None:
        self.threshold = threshold
        self._median: np.ndarray | None = None
        self._mad: np.ndarray | None = None

    def fit(self, X: np.ndarray) -> "MADDetector":
        self._median = np.median(X, axis=0)
        self._mad = np.median(np.abs(X - self._median), axis=0)
        self._mad = np.where(self._mad == 0, 1.0, self._mad)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Return -1 for anomaly, 1 for normal."""
        modified_z = 0.6745 * np.abs(X - self._median) / self._mad
        return np.where(modified_z.max(axis=1) > self.threshold, -1, 1)

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        """Return negative max modified-Z score (lower = more anomalous)."""
        modified_z = 0.6745 * np.abs(X - self._median) / self._mad
        return -modified_z.max(axis=1)
