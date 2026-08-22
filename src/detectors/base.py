"""
base.py — Common detector interface for AEGIS anomaly detectors (Phase 1, contract C3).

Every detector — the statistical baselines here, Phase 2's tripwire detector, and
Phase 4's graph-aware models — implements this interface so evaluation/ and the
dashboard can treat them interchangeably without hand-editing a new integration
each time a detector is added.

sklearn convention (every implementation must honor this):
    predict()        -> -1 for anomaly, 1 for normal
    score_samples()   -> lower score = more anomalous
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class BaseDetector(ABC):
    """Abstract base class every AEGIS anomaly detector must implement."""

    @abstractmethod
    def fit(self, X: np.ndarray) -> "BaseDetector":
        """Fit the detector on training data. Returns self."""
        raise NotImplementedError

    @abstractmethod
    def predict(self, X: np.ndarray) -> np.ndarray:
        """Return -1 for anomaly, 1 for normal (sklearn convention)."""
        raise NotImplementedError

    @abstractmethod
    def score_samples(self, X: np.ndarray) -> np.ndarray:
        """Return anomaly scores; lower = more anomalous (sklearn convention)."""
        raise NotImplementedError
