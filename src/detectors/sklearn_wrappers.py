"""
sklearn_wrappers.py — BaseDetector adapters for the sklearn-native detectors
in ml_engine.py (AEGIS Phase 3, extends contract C3's registry to cover
Isolation Forest and One-Class SVM).

ml_engine.train_isolation_forest / train_ocsvm_baseline are the canonical
implementations — this module does not reimplement fit logic, it only wraps
the already-fitted estimator behind the BaseDetector interface (fit/predict/
score_samples) so evaluation.run_evaluation() can iterate detectors/registry.py
instead of hardcoding a call to each function. Existing direct callers
(aegis_demo.py's ML Inspector tab, core/pipeline.run_analysis) are untouched
and keep calling train_isolation_forest directly — this module is additive.

Both sklearn.ensemble.IsolationForest and sklearn.svm.OneClassSVM already
honor the project's sklearn sign convention natively (predict: -1 anomaly /
1 normal; score_samples: lower = more anomalous), so no score inversion is
needed here.
"""
from __future__ import annotations

import numpy as np

from detectors.base import BaseDetector
from ml_engine import train_isolation_forest, train_ocsvm_baseline


class IsolationForestDetector(BaseDetector):
    """BaseDetector wrapper around ml_engine.train_isolation_forest.

    Follows the optional-override signature convention: constructor
    arguments default to None and fall back to SETTINGS.ml values inside
    train_isolation_forest itself.
    """

    def __init__(
        self,
        n_estimators: int | None = None,
        contamination: float | None = None,
        random_state: int | None = None,
    ) -> None:
        self.n_estimators = n_estimators
        self.contamination = contamination
        self.random_state = random_state
        self._clf = None

    def fit(self, X: np.ndarray) -> "IsolationForestDetector":
        self._clf = train_isolation_forest(
            X,
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=self.random_state,
        )
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self._clf.predict(X)

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        return self._clf.score_samples(X)


class OneClassSVMDetector(BaseDetector):
    """BaseDetector wrapper around ml_engine.train_ocsvm_baseline.

    ``nu``/``kernel`` mirror train_ocsvm_baseline's own defaults (0.08,
    "rbf") since that function does not (yet) draw them from SETTINGS.
    """

    def __init__(self, nu: float | None = None, kernel: str | None = None) -> None:
        self.nu = nu if nu is not None else 0.08
        self.kernel = kernel if kernel is not None else "rbf"
        self._clf = None

    def fit(self, X: np.ndarray) -> "OneClassSVMDetector":
        self._clf = train_ocsvm_baseline(X, nu=self.nu, kernel=self.kernel)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self._clf.predict(X)

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        return self._clf.score_samples(X)
