"""
test_detectors.py — Unit tests for the detector protocol and registry
(Phase 1, contract C3).

Tests cover:
- BaseDetector cannot be instantiated directly (it's abstract)
- ZScoreDetector / MADDetector conform to BaseDetector
- ml_engine re-exports the same class objects (not copies)
- The registry contains both baseline detectors and supports registration
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np

from detectors.base import BaseDetector
from detectors.sklearn_wrappers import IsolationForestDetector, OneClassSVMDetector
from detectors.statistical import MADDetector, ZScoreDetector
from detectors.registry import DETECTORS, register_detector


class TestBaseDetectorIsAbstract(unittest.TestCase):

    def test_cannot_instantiate_base_detector_directly(self):
        with self.assertRaises(TypeError):
            BaseDetector()


class TestStatisticalDetectorsConformToProtocol(unittest.TestCase):

    def setUp(self):
        rng = np.random.default_rng(42)
        self.X = rng.normal(size=(200, 3))

    def test_zscore_is_base_detector_subclass(self):
        self.assertTrue(issubclass(ZScoreDetector, BaseDetector))

    def test_mad_is_base_detector_subclass(self):
        self.assertTrue(issubclass(MADDetector, BaseDetector))

    def test_zscore_predict_sklearn_convention(self):
        clf = ZScoreDetector().fit(self.X)
        preds = clf.predict(self.X)
        self.assertTrue(set(preds.tolist()).issubset({-1, 1}))

    def test_mad_predict_sklearn_convention(self):
        clf = MADDetector().fit(self.X)
        preds = clf.predict(self.X)
        self.assertTrue(set(preds.tolist()).issubset({-1, 1}))

    def test_fit_returns_self(self):
        clf = ZScoreDetector()
        self.assertIs(clf.fit(self.X), clf)


class TestMlEngineReExport(unittest.TestCase):
    """ml_engine.py must re-export the same class objects, not duplicate
    definitions — CI's duplicate-def check only walks src/*.py directly, so
    this needs its own test to catch a divergent copy."""

    def test_ml_engine_zscore_is_same_class(self):
        from ml_engine import ZScoreDetector as ZFromMlEngine
        self.assertIs(ZFromMlEngine, ZScoreDetector)

    def test_ml_engine_mad_is_same_class(self):
        from ml_engine import MADDetector as MFromMlEngine
        self.assertIs(MFromMlEngine, MADDetector)


class TestSklearnWrapperDetectors(unittest.TestCase):
    """BaseDetector wrappers around ml_engine's sklearn-native trainers
    (Phase 3, extends contract C3's registry to Isolation Forest / OCSVM)."""

    def setUp(self):
        rng = np.random.default_rng(42)
        self.X = rng.normal(size=(200, 3))

    def test_isolation_forest_is_base_detector_subclass(self):
        self.assertTrue(issubclass(IsolationForestDetector, BaseDetector))

    def test_ocsvm_is_base_detector_subclass(self):
        self.assertTrue(issubclass(OneClassSVMDetector, BaseDetector))

    def test_isolation_forest_no_required_args(self):
        # Registry contract: constructible with no required arguments.
        IsolationForestDetector()

    def test_ocsvm_no_required_args(self):
        OneClassSVMDetector()

    def test_isolation_forest_predict_sklearn_convention(self):
        clf = IsolationForestDetector().fit(self.X)
        preds = clf.predict(self.X)
        self.assertTrue(set(preds.tolist()).issubset({-1, 1}))

    def test_isolation_forest_fit_returns_self(self):
        clf = IsolationForestDetector()
        self.assertIs(clf.fit(self.X), clf)

    def test_isolation_forest_score_samples_lower_is_more_anomalous(self):
        clf = IsolationForestDetector().fit(self.X)
        outlier = np.array([[100.0, 100.0, 100.0]])
        normal_score = clf.score_samples(self.X[:1])[0]
        outlier_score = clf.score_samples(outlier)[0]
        self.assertLess(outlier_score, normal_score)

    def test_ocsvm_predict_sklearn_convention(self):
        clf = OneClassSVMDetector().fit(self.X)
        preds = clf.predict(self.X)
        self.assertTrue(set(preds.tolist()).issubset({-1, 1}))

    def test_ocsvm_fit_returns_self(self):
        clf = OneClassSVMDetector()
        self.assertIs(clf.fit(self.X), clf)


class TestDetectorRegistry(unittest.TestCase):

    def test_registry_contains_baseline_detectors(self):
        self.assertIn("zscore", DETECTORS)
        self.assertIn("mad", DETECTORS)
        self.assertIs(DETECTORS["zscore"], ZScoreDetector)
        self.assertIs(DETECTORS["mad"], MADDetector)

    def test_registry_contains_sklearn_wrapped_detectors(self):
        self.assertIn("isolation_forest", DETECTORS)
        self.assertIn("ocsvm", DETECTORS)
        self.assertIs(DETECTORS["isolation_forest"], IsolationForestDetector)
        self.assertIs(DETECTORS["ocsvm"], OneClassSVMDetector)

    def test_registry_contains_tripwire(self):
        from deception.tripwire import TripwireDetector
        self.assertIn("tripwire", DETECTORS)
        self.assertIs(DETECTORS["tripwire"], TripwireDetector)

    def test_register_detector_adds_entry(self):
        class _DummyDetector(BaseDetector):
            def fit(self, X):
                return self

            def predict(self, X):
                return np.ones(len(X))

            def score_samples(self, X):
                return np.zeros(len(X))

        register_detector("dummy_for_test", _DummyDetector)
        self.assertIn("dummy_for_test", DETECTORS)
        self.assertIs(DETECTORS["dummy_for_test"], _DummyDetector)
        del DETECTORS["dummy_for_test"]  # don't leak into other tests


if __name__ == "__main__":
    unittest.main()
