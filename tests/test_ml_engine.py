"""
test_ml_engine.py — Unit tests for the ML anomaly detection engine.

Tests cover:
- Feature preprocessing: correct shape, StandardScaler is fitted
- IsolationForest training: model is fitted, produces correct output
- Anomaly scoring: correct columns added, flags match predictions
- Asset name mapping: IPs correctly resolved to asset names
- Model hyperparameter summary: returns expected structure
"""

import sys
import os
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from settings import SETTINGS
from ml_engine import (
    preprocess_features,
    train_isolation_forest,
    compute_anomaly_scores,
    add_asset_names,
    model_hyperparameters,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _sample_edges_df(n=50, seed=42):
    """Generate a small synthetic edges DataFrame for ML tests."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "source": [f"10.0.1.{i % 10}" for i in range(n)],
        "target": [f"10.0.1.{(i + 1) % 10}" for i in range(n)],
        "duration_sec": rng.exponential(scale=1.5, size=n),
        "packets": (rng.exponential(scale=100, size=n) + 5).astype(int),
        "bytes": rng.integers(500, 5_000_000, size=n),
        "timestamp": ["2026-01-01 00:00:00"] * n,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPreprocessFeatures(unittest.TestCase):

    def test_output_shape_matches_input_rows(self):
        df = _sample_edges_df()
        X_scaled, _ = preprocess_features(df)
        self.assertEqual(X_scaled.shape[0], len(df))

    def test_output_columns_match_default_features(self):
        df = _sample_edges_df()
        X_scaled, _ = preprocess_features(df)
        n_features = len(SETTINGS.ml.default_features)
        self.assertEqual(X_scaled.shape[1], n_features)

    def test_custom_features_override(self):
        df = _sample_edges_df()
        X_scaled, _ = preprocess_features(df, features=["duration_sec", "packets"])
        self.assertEqual(X_scaled.shape[1], 2)

    def test_scaler_is_fitted(self):
        """StandardScaler should have mean_ and scale_ attributes after fitting."""
        df = _sample_edges_df()
        _, scaler = preprocess_features(df)
        self.assertTrue(hasattr(scaler, "mean_"))
        self.assertTrue(hasattr(scaler, "scale_"))

    def test_missing_feature_column_raises(self):
        df = _sample_edges_df().drop(columns=["bytes"])
        with self.assertRaises((KeyError, ValueError)):
            preprocess_features(df)

    def test_does_not_mutate_input(self):
        df = _sample_edges_df()
        original_columns = set(df.columns)
        preprocess_features(df)
        self.assertEqual(set(df.columns), original_columns)

    def test_given_scaler_is_returned_unchanged_not_refit(self):
        """Passing a pre-fitted scaler must transform() with it, not fit a
        new one — the mean_/scale_ must be byte-identical before and after,
        the same invariant test_streaming_scorer.py pins for the streaming
        path (Invariant B), now also true of this shared helper."""
        train_df = _sample_edges_df(n=30, seed=1)
        eval_df = _sample_edges_df(n=20, seed=2)

        _, fitted_scaler = preprocess_features(train_df)
        mean_before = fitted_scaler.mean_.copy()
        scale_before = fitted_scaler.scale_.copy()

        _, returned_scaler = preprocess_features(eval_df, scaler=fitted_scaler)

        self.assertIs(returned_scaler, fitted_scaler)
        np.testing.assert_array_equal(fitted_scaler.mean_, mean_before)
        np.testing.assert_array_equal(fitted_scaler.scale_, scale_before)

    def test_given_scaler_transforms_using_its_own_statistics(self):
        """A row scaled with a pre-fitted scaler must match calling
        scaler.transform() directly — proving preprocess_features actually
        delegates rather than silently refitting."""
        train_df = _sample_edges_df(n=30, seed=1)
        eval_df = _sample_edges_df(n=20, seed=2)

        _, fitted_scaler = preprocess_features(train_df)
        X_eval_via_helper, _ = preprocess_features(eval_df, scaler=fitted_scaler)

        raw = eval_df[list(SETTINGS.ml.default_features)].astype(float)
        X_eval_direct = fitted_scaler.transform(raw)

        np.testing.assert_array_almost_equal(X_eval_via_helper, X_eval_direct)

    def test_omitting_scaler_still_fits_a_new_one(self):
        """Default (scaler=None) behavior is unchanged — every existing
        caller that never passes scaler= keeps fitting fresh."""
        df = _sample_edges_df()
        X_scaled, scaler = preprocess_features(df)
        self.assertTrue(hasattr(scaler, "mean_"))
        np.testing.assert_array_almost_equal(scaler.mean_, df[list(SETTINGS.ml.default_features)].astype(float).mean().values)


class TestTrainIsolationForest(unittest.TestCase):

    def setUp(self):
        df = _sample_edges_df()
        self.X_scaled, _ = preprocess_features(df)

    def test_model_produces_predictions(self):
        clf = train_isolation_forest(self.X_scaled)
        preds = clf.predict(self.X_scaled)
        # IsolationForest predict returns only -1 or 1.
        self.assertTrue(set(preds).issubset({-1, 1}))

    def test_custom_estimators_override(self):
        clf = train_isolation_forest(self.X_scaled, n_estimators=50)
        self.assertEqual(clf.n_estimators, 50)

    def test_default_settings_used_when_none(self):
        clf = train_isolation_forest(self.X_scaled)
        self.assertEqual(clf.n_estimators, SETTINGS.ml.isolation_forest_n_estimators)

    def test_predictions_length_matches_input(self):
        clf = train_isolation_forest(self.X_scaled)
        preds = clf.predict(self.X_scaled)
        self.assertEqual(len(preds), len(self.X_scaled))


class TestComputeAnomalyScores(unittest.TestCase):

    def setUp(self):
        self.df = _sample_edges_df()
        X_scaled, _ = preprocess_features(self.df)
        self.clf = train_isolation_forest(X_scaled)
        self.X_scaled = X_scaled

    def test_anomaly_score_column_added(self):
        result = compute_anomaly_scores(self.clf, self.X_scaled, self.df)
        self.assertIn("anomaly_score", result.columns)

    def test_raw_score_column_added(self):
        result = compute_anomaly_scores(self.clf, self.X_scaled, self.df)
        self.assertIn("raw_score", result.columns)

    def test_is_anomaly_column_added(self):
        result = compute_anomaly_scores(self.clf, self.X_scaled, self.df)
        self.assertIn("is_anomaly", result.columns)

    def test_is_anomaly_is_boolean(self):
        result = compute_anomaly_scores(self.clf, self.X_scaled, self.df)
        self.assertEqual(result["is_anomaly"].dtype, bool)

    def test_is_anomaly_consistent_with_anomaly_score(self):
        """is_anomaly must be True exactly when anomaly_score == -1."""
        result = compute_anomaly_scores(self.clf, self.X_scaled, self.df)
        mask = result["anomaly_score"] == -1
        self.assertTrue((result["is_anomaly"] == mask).all())

    def test_does_not_mutate_original_df(self):
        original_cols = set(self.df.columns)
        compute_anomaly_scores(self.clf, self.X_scaled, self.df)
        self.assertEqual(set(self.df.columns), original_cols)

    def test_output_length_matches_input(self):
        result = compute_anomaly_scores(self.clf, self.X_scaled, self.df)
        self.assertEqual(len(result), len(self.df))


class TestAddAssetNames(unittest.TestCase):

    def test_known_ip_mapped_correctly(self):
        df = pd.DataFrame({"source": ["10.0.1.10"], "target": ["10.0.1.12"]})
        ip_map = {"10.0.1.10": "Traffic_Cam_1", "10.0.1.12": "Traffic_Controller"}
        result = add_asset_names(df, ip_map)
        self.assertEqual(result["source_asset"].iloc[0], "Traffic_Cam_1")
        self.assertEqual(result["target_asset"].iloc[0], "Traffic_Controller")

    def test_unknown_ip_falls_back_to_ip(self):
        df = pd.DataFrame({"source": ["99.99.99.99"], "target": ["10.0.1.10"]})
        ip_map = {"10.0.1.10": "Traffic_Cam_1"}
        result = add_asset_names(df, ip_map)
        self.assertEqual(result["source_asset"].iloc[0], "99.99.99.99")

    def test_output_has_new_columns(self):
        df = pd.DataFrame({"source": ["10.0.1.10"], "target": ["10.0.1.12"]})
        result = add_asset_names(df, {})
        self.assertIn("source_asset", result.columns)
        self.assertIn("target_asset", result.columns)


class TestModelHyperparameters(unittest.TestCase):

    def test_returns_dict_with_required_keys(self):
        params = model_hyperparameters()
        self.assertIn("algorithm", params)
        self.assertIn("n_estimators", params)
        self.assertIn("contamination", params)
        self.assertIn("features_used", params)

    def test_defaults_match_settings(self):
        params = model_hyperparameters()
        self.assertEqual(params["n_estimators"], SETTINGS.ml.isolation_forest_n_estimators)
        self.assertEqual(params["contamination"], SETTINGS.ml.isolation_forest_contamination)

    def test_custom_values_reflected(self):
        params = model_hyperparameters(n_estimators=200, contamination=0.1)
        self.assertEqual(params["n_estimators"], 200)
        self.assertAlmostEqual(params["contamination"], 0.1)


if __name__ == "__main__":
    unittest.main()
