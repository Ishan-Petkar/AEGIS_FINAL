"""
tripwire.py — Deception-based tripwire detector for AEGIS Phase 2.

Fires deterministically when a connection uses a honeytoken credential
(config.HONEYTOKEN_CREDENTIALS) — a credential with zero legitimate use
anywhere in the system (see config.py's Phase 2 header comment). Unlike the
statistical/ML detectors in detectors/statistical.py, this is not a learned
pattern: any honeytoken touch is unambiguous compromise by construction, so
fit() is a no-op and predict()/score_samples() read a boolean
"is_honeytoken_use" signal directly rather than a learned distribution.

Preserves the sklearn convention every AEGIS detector honors (detectors/
base.py, contract C3): predict() -> -1 anomaly / 1 normal; score_samples()
-> lower = more anomalous.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from detectors.base import BaseDetector
from settings import SETTINGS


class TripwireDetector(BaseDetector):
    """Deterministic honeytoken-use detector.

    X is expected to be a 2D array whose first column is the
    "is_honeytoken_use" signal (1.0/True = the row touched a honeytoken
    credential, 0.0/False = it did not). Use `TripwireDetector.features_from_df`
    to build X from a DataFrame that may carry an `is_honeytoken_use` column.
    """

    def __init__(self, tripwire_score: float | None = None) -> None:
        self.tripwire_score = (
            tripwire_score if tripwire_score is not None else SETTINGS.deception.tripwire_score
        )

    @staticmethod
    def features_from_df(df: pd.DataFrame) -> np.ndarray:
        """Extract the is_honeytoken_use column as an (n, 1) float array.

        Rows without the column at all (ordinary volumetric traffic that
        never touched src/deception/adapter.py) are treated as False — they
        never used a honeytoken.
        """
        if "is_honeytoken_use" in df.columns:
            col = df["is_honeytoken_use"].fillna(False).astype(bool).astype(float)
        else:
            col = pd.Series(0.0, index=df.index)
        return col.to_numpy().reshape(-1, 1)

    def fit(self, X: np.ndarray) -> "TripwireDetector":
        """No-op: the tripwire fires on credential match, not a learned pattern."""
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Return -1 (anomaly) for rows where is_honeytoken_use is truthy, else 1."""
        flags = np.asarray(X, dtype=float)[:, 0] > 0.5
        return np.where(flags, -1, 1)

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        """Return a very negative score for honeytoken rows, 0.0 otherwise."""
        flags = np.asarray(X, dtype=float)[:, 0] > 0.5
        return np.where(flags, self.tripwire_score, 0.0)
