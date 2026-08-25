"""
tests/test_streaming_scorer.py — Phase 5 Ticket #5: `backend.streaming` /
`backend.supervised_detector`.

Two classes of test, matching the repo's existing convention
(tests/test_replay_reader.py, tests/test_replay_engine.py):

1. Offline (synthetic `ReplayFlow` fixtures, no dataset, no DB) — must run
   in CI. This is the bulk of the file, and it's what pins Invariant B.
2. Needs real data (`datasets/TrafficLabelling `) — `pytest.skip()`s
   cleanly via `_require_real_dataset()` if absent.
"""
from __future__ import annotations

import inspect
import random
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from backend.config import BackendSettings  # noqa: E402
from backend.replay_reader import ReplayFlow, ReplayFlowReader  # noqa: E402
from backend.streaming import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    ScoredFlow,
    StreamingScorer,
    StreamingScorerArtifactMissing,
    StreamingScorerError,
    StreamingScorerIncompatible,
    StreamingScorerNotFitted,
    fuse_tripwire_confidence,
)
from backend.supervised_detector import (  # noqa: E402
    BACKEND_DETECTORS,
    DETECTOR_NAME,
    SUPERVISED_FEATURE_NAMES,
    SupervisedFlowDetector,
    temporal_split_evaluate,
)
from datasets.loader import DatasetNotAvailable  # noqa: E402
from detectors.base import BaseDetector  # noqa: E402
from detectors.registry import DETECTORS  # noqa: E402
from core.pipeline import run_analysis  # noqa: E402
from settings import SETTINGS  # noqa: E402
import ml_engine  # noqa: E402

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_flow(
    i: int,
    duration_sec: float = 0.1,
    packets: int = 5,
    bytes_: int = 100,
    label: str = "BENIGN",
    bwd_packet_length_mean: float = 0.0,
    init_win_bytes_forward: int = 0,
    init_win_bytes_backward: int = 0,
    average_packet_size: float = 0.0,
) -> ReplayFlow:
    return ReplayFlow(
        ts=datetime(2017, 7, 3, 8, 0, 0, tzinfo=UTC) + timedelta(seconds=i),
        source_ip="10.0.0.1",
        source_port=1234,
        destination_ip="10.0.0.9",
        destination_port=80,
        protocol="TCP",
        duration_sec=duration_sec,
        packets=packets,
        bytes=bytes_,
        label=label,
        is_attack=label.strip().upper() != "BENIGN",
        timing_provenance="capture_seconds",
        source_row_id=f"synthetic:{i}",
        source_dataset="synthetic",
        bwd_packet_length_mean=bwd_packet_length_mean,
        init_win_bytes_forward=init_win_bytes_forward,
        init_win_bytes_backward=init_win_bytes_backward,
        average_packet_size=average_packet_size,
    )


def _benign_warmup_flows(n: int = 2000, seed: int = 42) -> list[ReplayFlow]:
    rng = random.Random(seed)
    return [
        _make_flow(
            i,
            duration_sec=rng.uniform(0.01, 5.0),
            packets=rng.randint(1, 30),
            bytes_=rng.randint(40, 900),
        )
        for i in range(n)
    ]


@pytest.fixture
def fitted_scorer() -> StreamingScorer:
    return StreamingScorer().fit_from_warmup(flows=_benign_warmup_flows())


def _require_real_dataset() -> ReplayFlowReader:
    reader = ReplayFlowReader()
    if not reader.data_dir.exists():
        pytest.skip(
            f"CIC-IDS2017 TrafficLabelling dataset not found at {reader.data_dir}; "
            "see docs/DATASETS.md."
        )
    return reader


# ---------------------------------------------------------------------------
# Invariant B — pinned hard (tests 1 & 2)
# ---------------------------------------------------------------------------


class TestInvariantB:
    def test_invariant_b_scaler_identity(self, fitted_scorer):
        scorer = fitted_scorer
        scaler = scorer._scaler
        scaler_id = id(scaler)
        mean_before = scaler.mean_.copy()
        scale_before = scaler.scale_.copy()
        var_before = scaler.var_.copy()

        extreme = [_make_flow(90_000 + i, duration_sec=999.0, packets=2_000_000, bytes_=9_000_000) for i in range(5)]
        normal_batches = [_benign_warmup_flows(n=20, seed=s) for s in range(50)]

        for batch in normal_batches:
            scorer.score_batch(batch)
        scorer.score_batch(extreme)

        assert id(scorer._scaler) == scaler_id, "score_batch must reuse the SAME scaler object"
        assert np.array_equal(scorer._scaler.mean_, mean_before)
        assert np.array_equal(scorer._scaler.scale_, scale_before)
        assert np.array_equal(scorer._scaler.var_, var_before)

    def test_invariant_b_no_fit_calls_in_scoring_path(self, fitted_scorer, monkeypatch):
        scorer = fitted_scorer

        def _boom(*args, **kwargs):
            raise AssertionError("fit/fit_transform must never be called from a scoring path")

        monkeypatch.setattr(StandardScaler, "fit", _boom)
        monkeypatch.setattr(StandardScaler, "fit_transform", _boom)
        monkeypatch.setattr(IsolationForest, "fit", _boom)
        monkeypatch.setattr(ml_engine, "preprocess_features", _boom)

        flows = _benign_warmup_flows(n=10, seed=7)
        scored = scorer.score_batch(flows)
        assert len(scored) == 10
        _ = scorer.score_event(flows[0])
        _ = scorer.explain(scored[0])


# ---------------------------------------------------------------------------
# score_batch / score_event behaviour
# ---------------------------------------------------------------------------


class TestScoring:
    def test_score_batch_equals_score_event(self, fitted_scorer):
        flows = _benign_warmup_flows(n=15, seed=3)
        batch_result = fitted_scorer.score_batch(flows)
        event_result = [fitted_scorer.score_event(f) for f in flows]
        assert batch_result == event_result

    def test_score_batch_order_and_length(self, fitted_scorer):
        flows = _benign_warmup_flows(n=8, seed=9)
        result = fitted_scorer.score_batch(flows)
        assert len(result) == len(flows)
        assert [r.flow for r in result] == flows

    def test_score_batch_empty_returns_empty_list(self, fitted_scorer):
        assert fitted_scorer.score_batch([]) == []

    def test_determinism_same_input_scored_twice(self, fitted_scorer):
        flows = _benign_warmup_flows(n=12, seed=11)
        first = fitted_scorer.score_batch(flows)
        second = fitted_scorer.score_batch(flows)
        assert first == second

    def test_no_feature_name_warnings(self, fitted_scorer):
        flows = _benign_warmup_flows(n=25, seed=13)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            fitted_scorer.score_batch(flows)
        feature_name_warnings = [
            w for w in caught if "feature names" in str(w.message).lower()
        ]
        assert not feature_name_warnings


# ---------------------------------------------------------------------------
# explain()
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_z_matches_hand_computed(self):
        scorer = StreamingScorer(feature_names=["duration_sec", "packets", "bytes"])
        scorer.fit_from_warmup(flows=_benign_warmup_flows())
        # Overwrite the fitted scaler with hand-picked, known-non-degenerate
        # stats so the expected z-scores are hand-computable.
        scorer._scaler.mean_ = np.array([10.0, 10.0, 500.0])
        scorer._scaler.scale_ = np.array([2.0, 4.0, 100.0])
        scorer._baseline_degenerate = (False, False, False)

        flow = _make_flow(0, duration_sec=14.0, packets=2.0, bytes_=300.0)
        scored = scorer.score_batch([flow])[0]
        explanation = scorer.explain(scored)

        expected = {
            "duration_sec": (14.0 - 10.0) / 2.0,
            "packets": (2.0 - 10.0) / 4.0,
            "bytes": (300.0 - 500.0) / 100.0,
        }
        by_name = {f["name"]: f for f in explanation["features"]}
        for name, exp_z in expected.items():
            assert by_name[name]["z"] == pytest.approx(exp_z, abs=1e-12)
            assert isinstance(by_name[name]["z"], float)
            assert by_name[name]["direction"] == ("above" if exp_z > 0 else "below" if exp_z < 0 else "at")
            assert by_name[name]["degenerate_baseline"] is False

        # Sorted by |z| descending: bytes(2.0) > duration_sec(2.0) tie ok,
        # packets(-2.0). All three must be present, none truncated.
        assert len(explanation["features"]) == 3
        zs = [abs(f["z"]) for f in explanation["features"]]
        assert zs == sorted(zs, reverse=True)

        # JSON round-trip: no numpy scalars anywhere.
        import json

        json.dumps(explanation)

    def test_explain_zero_variance_feature(self):
        # Bypass warmup_min_rows by fitting directly against a hand-built
        # constant-column matrix via ml_engine, mirroring what
        # fit_from_warmup does internally, then wiring the result into a
        # StreamingScorer instance (constant 'packets' column => zero
        # variance -- Q3's degenerate case).
        flows = [_make_flow(i, duration_sec=1.0 + i * 0.01, packets=7, bytes_=100 + i) for i in range(1200)]
        df = pd.DataFrame(
            {"duration_sec": [f.duration_sec for f in flows], "packets": [f.packets for f in flows], "bytes": [f.bytes for f in flows]}
        )
        X_scaled, scaler = ml_engine.preprocess_features(df, features=["duration_sec", "packets", "bytes"])
        model = ml_engine.train_isolation_forest(X_scaled, contamination=0.01, random_state=42)

        scorer = StreamingScorer(feature_names=["duration_sec", "packets", "bytes"])
        scorer._scaler = scaler
        scorer._model = model
        scorer._baseline_degenerate = tuple(bool(v) for v in (scaler.var_ == 0))
        scorer._warmup_meta = {"day": "synthetic", "rows_used": len(flows)}
        scorer._hyperparameters = {"contamination": 0.01}
        scorer._library_versions = {}

        assert scorer._baseline_degenerate[1] is True  # packets column is constant

        flow = _make_flow(9999, duration_sec=1.0, packets=999, bytes_=100)
        scored = scorer.score_batch([flow])[0]
        explanation = scorer.explain(scored)
        by_name = {f["name"]: f for f in explanation["features"]}
        assert by_name["packets"]["z"] is None
        assert by_name["packets"]["degenerate_baseline"] is True
        assert "sigma" not in explanation["summary"] or explanation["top_feature"] != "packets"
        # top_feature prefers a non-degenerate feature when one exists.
        assert by_name[explanation["top_feature"]]["degenerate_baseline"] is False

    def test_explain_rejects_replay_flow(self, fitted_scorer):
        """LOW-1: passing a bare ReplayFlow (an easy confusion with
        ScoredFlow, e.g. Ticket #7 handles both) must raise a clear
        TypeError naming both types and score_batch()/score_event(),
        instead of an opaque AttributeError leaking explain()'s
        internals."""
        flow = _make_flow(0)
        with pytest.raises(TypeError, match="ScoredFlow"):
            fitted_scorer.explain(flow)


# ---------------------------------------------------------------------------
# fit_from_warmup() guard rails
# ---------------------------------------------------------------------------


class TestFitFromWarmup:
    def test_fit_rejects_too_few_rows(self):
        with pytest.raises(StreamingScorerError):
            StreamingScorer().fit_from_warmup(flows=_benign_warmup_flows(n=5))

    def test_fit_rejects_attack_rows(self):
        flows = _benign_warmup_flows(n=1500)
        flows[10] = _make_flow(10, label="Bot")
        with pytest.raises(StreamingScorerError):
            StreamingScorer().fit_from_warmup(flows=flows)

    def test_fit_records_warmup_metadata(self, fitted_scorer):
        meta = fitted_scorer.baseline["warmup"]
        assert meta["rows_used"] == 2000
        assert meta["attack_rows_in_warmup"] == 0
        assert "fitted_at" in meta


# ---------------------------------------------------------------------------
# save() / load()
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_load_round_trip(self, fitted_scorer, tmp_path):
        path = tmp_path / "scorer.joblib"
        fitted_scorer.save(path)
        flows = _benign_warmup_flows(n=30, seed=99)
        before = fitted_scorer.score_batch(flows)

        loaded = StreamingScorer.load(path)
        after = loaded.score_batch(flows)

        assert before == after
        assert loaded.feature_names == fitted_scorer.feature_names
        assert loaded._baseline_degenerate == fitted_scorer._baseline_degenerate
        assert loaded.baseline["warmup"] == fitted_scorer.baseline["warmup"]

    def test_save_uses_resolved_path(self, fitted_scorer, monkeypatch, tmp_path):
        # model_artifact_path_resolved (K2) is CWD-independent by
        # construction (backend/config.py: resolved against _REPO_ROOT,
        # derived from __file__, not the process CWD) -- mirrors
        # tests/test_backend_config.py's own pin on that property. This
        # test confirms save() actually USES the resolved property rather
        # than the raw (CWD-relative) model_artifact_path field.
        repo_root = Path(__file__).resolve().parent.parent
        target = repo_root / "artifacts" / "streaming_scorer.joblib"

        # This test genuinely writes to the repo's real artifacts/ path (that
        # is the point -- it pins K2). An operator may already have a warmup
        # artifact there, built via `python -m backend.warmup`, that the demo
        # depends on. Preserve and restore it: unconditionally deleting would
        # mean "run the tests before demoing" silently destroys the demo's
        # model, which fails at startup with a missing-artifact error.
        preexisting = target.read_bytes() if target.exists() else None

        monkeypatch.chdir(tmp_path)
        try:
            written = fitted_scorer.save()
            assert written.is_absolute()
            assert written == target
            # Nothing should have been written under tmp_path.
            assert not (tmp_path / "artifacts").exists()
        finally:
            if preexisting is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(preexisting)
            else:
                target.unlink(missing_ok=True)

    def test_load_missing_artifact_raises_and_does_not_fit(self, tmp_path, monkeypatch):
        missing_path = tmp_path / "does_not_exist.joblib"

        def _boom(*args, **kwargs):
            raise AssertionError("load() must never fit on a missing artifact")

        monkeypatch.setattr(IsolationForest, "fit", _boom)
        monkeypatch.setattr(StandardScaler, "fit", _boom)

        with pytest.raises(StreamingScorerArtifactMissing) as excinfo:
            StreamingScorer.load(missing_path)
        message = str(excinfo.value)
        assert str(missing_path) in message
        assert "backend.warmup" in message
        assert isinstance(excinfo.value, FileNotFoundError)

    def test_load_has_no_fit_if_missing_parameter(self):
        sig = inspect.signature(StreamingScorer.load)
        assert "fit_if_missing" not in sig.parameters
        assert "auto_fit" not in sig.parameters

    def test_load_rejects_incompatible_schema_version(self, fitted_scorer, tmp_path):
        import joblib

        path = tmp_path / "scorer.joblib"
        fitted_scorer.save(path)
        artifact = joblib.load(path)
        artifact["artifact_schema_version"] = "999.0"
        joblib.dump(artifact, path)
        with pytest.raises(StreamingScorerIncompatible):
            StreamingScorer.load(path)

    def test_load_rejects_incompatible_feature_names(self, fitted_scorer, tmp_path):
        import joblib

        path = tmp_path / "scorer.joblib"
        fitted_scorer.save(path)
        artifact = joblib.load(path)
        artifact["feature_names"] = ["duration_sec", "packets"]
        joblib.dump(artifact, path)
        with pytest.raises(StreamingScorerIncompatible):
            StreamingScorer.load(path)

    def test_load_rejects_incompatible_n_features(self, fitted_scorer, tmp_path):
        import joblib

        path = tmp_path / "scorer.joblib"
        fitted_scorer.save(path)
        artifact = joblib.load(path)
        # feature_names says 3, but n_features_in_ on the (real) scaler is
        # still 3 -- forge a mismatch directly.
        artifact["scaler"].n_features_in_ = 99
        joblib.dump(artifact, path)
        with pytest.raises(StreamingScorerIncompatible):
            StreamingScorer.load(path)


# ---------------------------------------------------------------------------
# Unfitted scorer
# ---------------------------------------------------------------------------


class TestUnfitted:
    def test_unfitted_scorer_raises(self):
        scorer = StreamingScorer()
        assert scorer.is_fitted is False
        flow = _make_flow(0)
        with pytest.raises(StreamingScorerNotFitted):
            scorer.score_batch([flow])
        with pytest.raises(StreamingScorerNotFitted):
            scorer.score_event(flow)
        with pytest.raises(StreamingScorerNotFitted):
            scorer.baseline


# ---------------------------------------------------------------------------
# Tripwire fusion (Invariant C)
# ---------------------------------------------------------------------------


class TestFusion:
    def test_fusion_truth_table_matches_settings(self):
        deception_cfg = SETTINGS.deception
        volume_fired = np.array([True, True, False, False])
        tripwire_fired = np.array([True, False, True, False])
        is_anomaly, confidence = fuse_tripwire_confidence(volume_fired, tripwire_fired)

        assert list(is_anomaly) == [True, True, True, False]
        assert confidence[0] == pytest.approx(deception_cfg.confidence_both)
        assert confidence[1] == pytest.approx(deception_cfg.confidence_volume_only)
        assert confidence[2] == pytest.approx(deception_cfg.confidence_tripwire_only)
        assert confidence[3] == pytest.approx(deception_cfg.confidence_none)

    def test_fusion_matches_pipeline(self):
        """Behavioural parity against core.pipeline.run_analysis's own
        fusion, using only its PUBLIC output columns (no reach into
        pipeline internals): is_anomaly = volume_fired | tripwire_fired
        and tripwire_fired is exposed directly, so volume_fired is fully
        recoverable from (tripwire_fired, is_anomaly, confidence) since
        confidence_both != confidence_tripwire_only.
        """
        result = run_analysis(dataset="deception", dataset_limit=300, random_seed=42)
        df = result.edges_df
        assert "is_honeytoken_use" in df.columns  # sanity: fixture carries the column

        deception_cfg = SETTINGS.deception
        tripwire_fired = df["tripwire_fired"].to_numpy()
        is_anomaly = df["is_anomaly"].to_numpy()
        confidence = df["confidence"].to_numpy()

        volume_fired = np.where(
            ~tripwire_fired,
            is_anomaly,
            np.isclose(confidence, deception_cfg.confidence_both),
        )

        recomputed_anomaly, recomputed_confidence = fuse_tripwire_confidence(
            volume_fired, tripwire_fired
        )
        assert np.array_equal(recomputed_anomaly, is_anomaly)
        assert np.allclose(recomputed_confidence, confidence)


# ---------------------------------------------------------------------------
# Contamination trap (upstream ml_engine behaviour, documented regression)
# ---------------------------------------------------------------------------


class TestContamination:
    def test_streaming_contamination_setting_rejects_zero(self):
        with pytest.raises(Exception):
            BackendSettings(streaming_contamination=0.0)

    def test_contamination_zero_silently_ignored_by_ml_engine(self):
        """Documents the upstream trap (docs/PHASE5_TICKET5_PLAN.md section
        6, plan doc error item 8): ml_engine.train_isolation_forest does
        `contamination or SETTINGS...`, so passing 0.0 does NOT mean 'no
        flagging' -- it silently falls back to the 0.08 default. This test
        exists so that trap can never silently change meaning."""
        X = np.random.RandomState(0).normal(size=(200, 3))
        clf = ml_engine.train_isolation_forest(X, contamination=0.0, random_state=0)
        assert clf.contamination == SETTINGS.ml.isolation_forest_contamination


# ---------------------------------------------------------------------------
# Supervised detector (Part B)
# ---------------------------------------------------------------------------


class TestSupervisedDetector:
    def test_conforms_to_base_detector(self):
        assert issubclass(SupervisedFlowDetector, BaseDetector)
        det = SupervisedFlowDetector()  # constructible with no required args
        assert isinstance(det, BaseDetector)

    def test_not_registered_in_global_c3_registry(self):
        """Ticket #5 HIGH-1 (docs/PHASE5_STATE.md K6): a supervised
        classifier must never be registered into the global
        detectors.registry.DETECTORS, because that registry's contract is
        "registered => benchmarked" -- evaluation.run_evaluation() fits
        every entry on a benign-only, label-free split and publishes the
        result into the Research Console's benchmark table. Importing
        backend.supervised_detector must leave DETECTORS exactly as the
        five pre-existing unsupervised/tripwire entries."""
        assert "supervised_flow" not in DETECTORS
        assert sorted(DETECTORS.keys()) == [
            "isolation_forest",
            "mad",
            "ocsvm",
            "tripwire",
            "zscore",
        ]

    def test_exposed_via_backend_local_map(self):
        assert BACKEND_DETECTORS[DETECTOR_NAME] is SupervisedFlowDetector
        assert DETECTOR_NAME == "supervised_flow"

    def test_sign_conventions_on_separable_data(self):
        rng = np.random.RandomState(0)
        X_benign = rng.normal(0, 1, size=(150, 4))
        X_attack = rng.normal(8, 1, size=(150, 4))
        X = np.vstack([X_benign, X_attack])
        y = np.array([0] * 150 + [1] * 150)

        det = SupervisedFlowDetector().fit(X, y)
        preds = det.predict(X)
        scores = det.score_samples(X)

        assert set(np.unique(preds)) <= {-1, 1}
        assert (preds[150:] == -1).mean() > 0.9  # attacks mostly flagged
        assert (preds[:150] == 1).mean() > 0.9  # benign mostly not flagged
        # lower score = more anomalous
        assert scores[150:].mean() < scores[:150].mean()

    def test_fit_without_labels_raises(self):
        """Ticket #5 HIGH-1 fix: with this detector no longer registered
        into the global C3 registry, there is no legitimate label-free
        caller left to accommodate, so a silently-degrading always-
        "normal" fallback is a trap, not a safety net. fit(X) with no y
        must raise, naming the correct entry points."""
        X = np.random.RandomState(1).normal(size=(50, 4))
        with pytest.raises(ValueError, match="fit\\(X, y\\)|temporal_split_evaluate"):
            SupervisedFlowDetector().fit(X)  # y=None

    def test_predict_before_fit_raises(self):
        X = np.random.RandomState(1).normal(size=(10, 4))
        det = SupervisedFlowDetector()
        with pytest.raises(RuntimeError):
            det.predict(X)
        with pytest.raises(RuntimeError):
            det.score_samples(X)

    def test_temporal_split_evaluate_real_data(self):
        _require_real_dataset()
        result = temporal_split_evaluate(day="friday-morning")
        assert result["n_train"] > 0 and result["n_test"] > 0
        assert result["n_test_attacks"] > 0
        assert 0.0 <= result["precision"] <= 1.0
        assert 0.0 <= result["recall"] <= 1.0
        assert 0.7 <= result["auc"] <= 1.0  # measured 0.847; sanity band
        assert result["precision"] >= 0.9  # measured 0.9979; known-threat precision is high
        assert "temporal_split" in result["method"]
        assert "self-test" in result["method"] or "same-distribution" in result["method"]


# ---------------------------------------------------------------------------
# Reader extension additivity (Ticket #5 Part B)
# ---------------------------------------------------------------------------


class TestReaderExtensionAdditive:
    def test_replay_flow_original_fields_unchanged(self):
        # Constructing via the ORIGINAL keyword set (no new fields) must
        # still work -- new fields must all have defaults.
        flow = ReplayFlow(
            ts=datetime(2017, 7, 3, 8, 0, 0, tzinfo=UTC),
            source_ip="10.0.0.1",
            source_port=1234,
            destination_ip="10.0.0.9",
            destination_port=80,
            protocol="TCP",
            duration_sec=0.1,
            packets=1,
            bytes=100,
            label="BENIGN",
            is_attack=False,
            timing_provenance="capture_seconds",
            source_row_id="synthetic:1",
            source_dataset="synthetic",
        )
        assert flow.bwd_packet_length_mean == 0.0
        assert flow.init_win_bytes_forward == 0
        assert flow.init_win_bytes_backward == 0
        assert flow.average_packet_size == 0.0

    def test_new_fields_present_and_named(self):
        names = {f.name for f in __import__("dataclasses").fields(ReplayFlow)}
        assert {
            "bwd_packet_length_mean",
            "init_win_bytes_forward",
            "init_win_bytes_backward",
            "average_packet_size",
        } <= names
        assert set(SUPERVISED_FEATURE_NAMES) <= names | {"duration_sec", "packets", "bytes"}


# ---------------------------------------------------------------------------
# Real-data tests (skip cleanly without datasets/)
# ---------------------------------------------------------------------------


class TestRealData:
    def test_warmup_day_is_all_benign(self):
        reader = _require_real_dataset()
        flows = list(reader.iter_flows(day="monday"))
        assert len(flows) > 500_000
        assert sum(1 for f in flows if f.is_attack) == 0
        stats = reader.last_read_stats
        assert stats.rows_seen == stats.rows_emitted

    def test_warmup_baseline_has_no_degenerate_features(self):
        _require_real_dataset()
        scorer = StreamingScorer().fit_from_warmup(day="monday")
        assert all(not d for d in scorer._baseline_degenerate)

    def test_warmup_fit_under_time_budget(self):
        _require_real_dataset()
        start = time.perf_counter()
        StreamingScorer().fit_from_warmup(day="monday")
        elapsed = time.perf_counter() - start
        assert elapsed < 30.0, f"fit_from_warmup took {elapsed:.1f}s, expected < 30s (measured 4.72s)"

    def test_flag_rate_on_landing_stream(self):
        reader = _require_real_dataset()
        scorer = StreamingScorer().fit_from_warmup(day="monday")
        friday = list(reader.iter_flows(day="friday-morning"))
        scored = scorer.score_batch(friday)
        flag_rate = sum(1 for s in scored if s.is_anomaly) / len(scored)
        assert 0.003 <= flag_rate <= 0.015, f"flag rate {flag_rate:.4%} outside the documented 0.3%-1.5% band"

    def test_detectability_is_recorded_not_assumed(self):
        """The volumetric channel's real, measured weakness on this
        dataset (docs/DETECTION_STUDY.md / docs/PHASE5_TICKET5_PLAN.md
        section 6) must stay visible in the test suite, not be quietly
        assumed away by a future change."""
        from sklearn.metrics import roc_auc_score

        reader = _require_real_dataset()
        scorer = StreamingScorer().fit_from_warmup(day="monday")
        friday = list(reader.iter_flows(day="friday-morning"))
        scored = scorer.score_batch(friday)
        y_true = [1 if s.flow.is_attack else 0 for s in scored]
        # lower raw_score = more anomalous -> negate for "higher = more anomalous"
        y_score = [-s.raw_score for s in scored]
        auc = roc_auc_score(y_true, y_score)
        assert 0.55 <= auc <= 0.70, (
            f"measured ROC AUC {auc:.3f} outside [0.55, 0.70] -- if this "
            "moved, look at WHY (feature set? landing day?) rather than "
            "widening the band."
        )
