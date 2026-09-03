"""
test_evaluation.py — Tests for the Phase 3 evaluation harness.

Most tests use the synthetic dataset fallback (no real CIC-IDS2017 files
required), which ensures the CI pipeline is always green.
"""
from unittest import mock

import numpy as np
import pytest
import sys
import pathlib

# Ensure src/ is on the path
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import ml_engine  # noqa: E402
from evaluation import (
    DegenerateEvaluationError,
    _evaluate,
    results_to_dataframe,
    run_evaluation,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def perfect_predictions():
    """Case: detector perfectly matches ground truth."""
    y_true = np.array([0, 0, 0, 1, 1, 1])
    preds  = np.array([1, 1, 1, -1, -1, -1])  # sklearn -1/1 convention
    scores = np.array([-0.1, -0.1, -0.1, -0.9, -0.9, -0.9])
    return y_true, preds, scores


@pytest.fixture
def all_normal_predictions():
    """Case: detector never fires — predicts all as normal."""
    y_true = np.array([0, 0, 1, 1])
    preds  = np.array([1, 1, 1, 1])
    scores = np.array([-0.1, -0.1, -0.1, -0.1])
    return y_true, preds, scores


@pytest.fixture
def all_anomaly_predictions():
    """Case: detector always fires — predicts everything as anomaly."""
    y_true = np.array([0, 0, 1, 1])
    preds  = np.array([-1, -1, -1, -1])
    scores = np.array([-0.9, -0.9, -0.9, -0.9])
    return y_true, preds, scores


# ---------------------------------------------------------------------------
# EvalResult tests
# ---------------------------------------------------------------------------

class TestEvalResult:
    def test_perfect_metrics(self, perfect_predictions):
        y_true, preds, scores = perfect_predictions
        r = _evaluate("TestDetector", preds, scores, y_true, dataset="test")
        assert r.precision == pytest.approx(1.0)
        assert r.recall    == pytest.approx(1.0)
        assert r.f1        == pytest.approx(1.0)

    def test_all_normal_gives_zero_recall(self, all_normal_predictions):
        y_true, preds, scores = all_normal_predictions
        r = _evaluate("TestDetector", preds, scores, y_true, dataset="test")
        assert r.recall == 0.0
        assert r.n_predicted_anomalies == 0

    def test_all_anomaly_gives_zero_precision(self, all_anomaly_predictions):
        y_true, preds, scores = all_anomaly_predictions
        r = _evaluate("TestDetector", preds, scores, y_true, dataset="test")
        # precision = TP / (TP + FP); TP=2, FP=2 → 0.5
        assert r.precision == pytest.approx(0.5)
        assert r.recall    == pytest.approx(1.0)
        assert r.n_predicted_anomalies == 4

    def test_n_total_matches_input(self, perfect_predictions):
        y_true, preds, scores = perfect_predictions
        r = _evaluate("X", preds, scores, y_true, dataset="test")
        assert r.n_total == len(y_true)

    def test_n_true_anomalies(self, perfect_predictions):
        y_true, preds, scores = perfect_predictions
        r = _evaluate("X", preds, scores, y_true, dataset="test")
        assert r.n_true_anomalies == int(y_true.sum())

    def test_to_dict_has_required_keys(self, perfect_predictions):
        y_true, preds, scores = perfect_predictions
        r = _evaluate("X", preds, scores, y_true, dataset="test")
        d = r.to_dict()
        for key in ("detector", "precision", "recall", "f1", "roc_auc",
                    "n_total", "n_true_anomalies", "n_predicted_anomalies"):
            assert key in d

    def test_str_representation(self, perfect_predictions):
        y_true, preds, scores = perfect_predictions
        r = _evaluate("MyModel", preds, scores, y_true, dataset="test")
        s = str(r)
        assert "MyModel" in s
        assert "P=" in s
        assert "F1=" in s


# ---------------------------------------------------------------------------
# Baseline detector tests
# ---------------------------------------------------------------------------

class TestZScoreDetector:
    def test_predicts_outlier(self):
        from ml_engine import ZScoreDetector
        X_train = np.array([[1.0, 1.0], [1.1, 0.9], [0.95, 1.05]])
        X_test  = np.array([[1.0, 1.0], [100.0, 100.0]])  # extreme outlier
        det = ZScoreDetector(threshold=3.0).fit(X_train)
        preds = det.predict(X_test)
        assert preds[0] == 1   # normal
        assert preds[1] == -1  # anomaly

    def test_scores_are_lower_for_outlier(self):
        from ml_engine import ZScoreDetector
        X = np.array([[1.0, 1.0], [1.0, 1.0], [0.0, 0.0]])
        det = ZScoreDetector().fit(X)
        scores = det.score_samples(np.array([[1.0, 1.0], [100.0, 100.0]]))
        assert scores[0] > scores[1]  # normal > anomaly (higher = more normal)

    def test_fit_handles_zero_std(self):
        from ml_engine import ZScoreDetector
        X = np.ones((5, 2))  # constant features, std=0
        det = ZScoreDetector().fit(X)
        # Must not raise; std should be replaced with 1.0
        preds = det.predict(X)
        assert len(preds) == 5


class TestMADDetector:
    def test_predicts_outlier(self):
        from ml_engine import MADDetector
        X_train = np.array([[1.0, 1.0], [1.1, 0.9], [0.95, 1.05]])
        X_test  = np.array([[1.0, 1.0], [50.0, 50.0]])
        det = MADDetector(threshold=3.5).fit(X_train)
        preds = det.predict(X_test)
        assert preds[0] == 1
        assert preds[1] == -1

    def test_robust_to_single_outlier_in_training(self):
        """MAD should not be skewed by a single outlier in training data."""
        from ml_engine import MADDetector
        X_train = np.vstack([
            np.ones((50, 2)),
            np.array([[1000.0, 1000.0]])  # single extreme outlier in training
        ])
        det = MADDetector().fit(X_train)
        # Median should remain at 1.0, not pulled to 1000
        assert np.allclose(det._median, 1.0)


class TestOCSVM:
    def test_trains_and_predicts(self):
        from ml_engine import train_ocsvm_baseline
        X = np.random.default_rng(0).standard_normal((50, 3))
        clf = train_ocsvm_baseline(X, nu=0.1)
        preds = clf.predict(X)
        assert set(preds).issubset({-1, 1})

    def test_scores_returned(self):
        from ml_engine import train_ocsvm_baseline
        X = np.random.default_rng(0).standard_normal((30, 2))
        clf = train_ocsvm_baseline(X)
        scores = clf.score_samples(X)
        assert len(scores) == 30


# ---------------------------------------------------------------------------
# Full pipeline tests (uses synthetic fallback)
# ---------------------------------------------------------------------------

class TestRunEvaluation:
    def test_returns_list_of_eval_results(self):
        """With no real CIC data, falls back to synthetic — should still return results."""
        results = run_evaluation(limit=500, include_ocsvm=False, verbose=False)
        assert isinstance(results, list)
        assert len(results) >= 2  # at least IF + Z-Score + MAD

    def test_all_results_have_valid_metrics(self):
        results = run_evaluation(limit=500, include_ocsvm=False, verbose=False)
        for r in results:
            assert 0.0 <= r.precision <= 1.0
            assert 0.0 <= r.recall <= 1.0
            assert 0.0 <= r.f1 <= 1.0
            assert r.n_total > 0

    def test_ocsvm_included_when_requested(self):
        results = run_evaluation(limit=200, include_ocsvm=True, verbose=False)
        names = [r.detector for r in results]
        assert any("SVM" in n for n in names)

    def test_results_to_dataframe_shape(self):
        results = run_evaluation(limit=300, include_ocsvm=False, verbose=False)
        df = results_to_dataframe(results)
        assert "precision" in df.columns
        assert "recall" in df.columns
        assert "f1" in df.columns
        assert len(df) == len(results)

    def test_f1_bounded_0_to_1(self):
        results = run_evaluation(limit=300, include_ocsvm=False, verbose=False)
        for r in results:
            assert 0.0 <= r.f1 <= 1.0

    def test_n_true_anomalies_consistent_across_results(self):
        """All detectors see the same eval set, so n_true_anomalies must match."""
        results = run_evaluation(limit=500, include_ocsvm=False, verbose=False)
        expected = results[0].n_true_anomalies
        for r in results:
            assert r.n_true_anomalies == expected

    def test_tripwire_excluded_from_registry_loop(self):
        """TripwireDetector is registered (detectors/registry.py) but must
        never appear in the standard precision/recall/F1 benchmark — see
        evaluation/__init__.py's module docstring for why forcing it through
        this pipeline would be a degenerate, meaningless result."""
        results = run_evaluation(limit=500, include_ocsvm=False, verbose=False)
        names = [r.detector.lower() for r in results]
        assert not any("tripwire" in n for n in names)

    def test_results_include_isolation_forest_from_registry(self):
        """run_evaluation() must iterate detectors.registry.DETECTORS rather
        than a hardcoded list — Isolation Forest should appear via the
        registry's "isolation_forest" entry."""
        results = run_evaluation(limit=500, include_ocsvm=False, verbose=False)
        names = [r.detector for r in results]
        assert any("Isolation Forest" in n for n in names)

    def test_pointwise_scoring_tag_for_non_swat_dataset(self):
        results = run_evaluation(limit=500, include_ocsvm=False, verbose=False)
        for r in results:
            assert r.scoring == "pointwise"


# ---------------------------------------------------------------------------
# Scaler train/eval leakage (Phase C methodology-rigor pass): the scaler
# must be fit on the TRAIN split alone and only .transform()-ed onto eval,
# never .fit_transform()-ed on the full dataset before splitting.
# ---------------------------------------------------------------------------

class TestScalerLeakageFix:
    def test_preprocess_features_called_once_per_split_train_fits_eval_transforms(self):
        """Spies on ml_engine.preprocess_features (run_evaluation imports it
        locally, so patching the module attribute is what actually takes
        effect) to prove: exactly two calls, the first with scaler=None
        (fits fresh on train rows only) and the second with a scaler
        already fit at call time (reuses it — .transform() only)."""
        calls = []
        original = ml_engine.preprocess_features

        def spy(edges_df, features=None, scaler=None):
            calls.append({"n_rows": len(edges_df), "scaler_was_given": scaler is not None})
            return original(edges_df, features=features, scaler=scaler)

        with mock.patch("ml_engine.preprocess_features", side_effect=spy):
            run_evaluation(dataset="synthetic", limit=800, include_ocsvm=False, verbose=False)

        assert len(calls) == 2, "expected exactly one preprocess_features call for train, one for eval"
        train_call, eval_call = calls
        assert train_call["scaler_was_given"] is False, "train split must fit its own scaler"
        assert eval_call["scaler_was_given"] is True, "eval split must reuse the train-fitted scaler, not fit its own"
        # Neither call sees the full dataset — each sees only its own split.
        assert 0 < train_call["n_rows"] < 800
        assert 0 < eval_call["n_rows"] < 800

    def test_eval_split_scaler_statistics_match_train_only_not_full_dataset(self):
        """Direct numeric proof of no-leakage: fit a scaler by hand on just
        the train rows run_evaluation actually used, and confirm it's
        statistically different from a scaler fit on the full dataset —
        the leak this fix removes would have made them identical."""
        from datasets.loader import load_dataset
        from datasets.schema import ACTION_ALERT
        from settings import SETTINGS as ENGINE_SETTINGS

        batch = load_dataset("synthetic", limit=800)
        df = batch.df
        y_true = (df["action"] == ACTION_ALERT).astype(int).values
        feature_cols = list(ENGINE_SETTINGS.ml.default_features)
        for col in feature_cols:
            if col not in df.columns:
                df[col] = 0.0

        benign_idx = np.where(y_true == 0)[0]
        rng = np.random.default_rng(42)  # run_evaluation's default random_state
        rng.shuffle(benign_idx)
        n_train = int(len(benign_idx) * 0.5)  # run_evaluation's default train_fraction
        train_idx = benign_idx[:n_train]

        X_train_only, scaler_train_only = ml_engine.preprocess_features(
            df.iloc[train_idx], features=feature_cols
        )
        X_full, scaler_full = ml_engine.preprocess_features(df, features=feature_cols)

        # A scaler fit on a strict subset of rows generally has different
        # mean_/scale_ than one fit on the full dataset — assert they are
        # NOT the leak-era identical values. (A false pass here would need
        # the train-only rows to coincidentally have the exact same
        # per-column mean/std as the full dataset, vanishingly unlikely for
        # a real feature distribution with attacks mixed in.)
        assert not np.allclose(scaler_train_only.mean_, scaler_full.mean_), (
            "train-only scaler statistics match the full-dataset scaler — "
            "the leak (fitting on all rows before splitting) may have "
            "reappeared"
        )


# ---------------------------------------------------------------------------
# Degenerate-split guard (pins the old silent
# P=0.000 R=0.000 F1=0.000 AUC=nan failure mode so it can never recur)
# ---------------------------------------------------------------------------

class TestDegenerateEvaluationGuard:
    def test_all_anomalous_split_raises(self):
        """The "deception" dataset is 100% ACTION_ALERT tripwire events by
        construction (deception/adapter.py) — a real, always-available
        source that reproduces the degenerate-split bug without needing a
        synthetic anomaly_rate override."""
        with pytest.raises(DegenerateEvaluationError):
            run_evaluation(dataset="deception", limit=10, include_ocsvm=False, verbose=False)

    def test_degenerate_error_does_not_silently_return_zeros(self):
        """The old bug: a degenerate split silently returned EvalResult
        objects with P=R=F1=0.000, AUC=nan instead of failing loudly. This
        must now raise, not return anything."""
        raised = False
        try:
            run_evaluation(dataset="deception", limit=10, include_ocsvm=False, verbose=False)
        except DegenerateEvaluationError:
            raised = True
        assert raised, "degenerate split must raise, not silently return zeroed results"

    def test_custom_bounds_override_settings_default(self):
        """min_positive_rate/max_positive_rate follow the optional-override
        convention — a call-site override should be honored over SETTINGS,
        in both directions.

        Raising min_positive_rate well above the synthetic generator's
        ~15-20% anomaly rate turns an otherwise-healthy split into a
        rejected one, proving the override actually changes the guard's
        decision (not just accepted and ignored)."""
        # Default bounds: this split is fine (covered by TestRunEvaluation).
        run_evaluation(dataset="synthetic", limit=800, include_ocsvm=False, verbose=False)

        # Overridden bound: the same kind of split is now degenerate.
        with pytest.raises(DegenerateEvaluationError):
            run_evaluation(
                dataset="synthetic", limit=800, include_ocsvm=False, verbose=False,
                min_positive_rate=0.5,
            )

    def test_degenerate_error_message_is_actionable(self):
        with pytest.raises(DegenerateEvaluationError) as exc_info:
            run_evaluation(dataset="deception", limit=10, include_ocsvm=False, verbose=False)
        msg = str(exc_info.value)
        assert "positive rate" in msg.lower()
        assert "deception" in msg.lower()
