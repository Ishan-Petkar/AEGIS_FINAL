"""
backend/supervised_detector.py — Phase 5 Ticket #5 Part B: the supervised
(known-threat) detection channel.

Justification (docs/DETECTION_STUDY.md)
----------------------------------------------------------------------------
Ticket #5 planning found the unsupervised `StreamingScorer` channel gets
precision ~0.02 on real replayed friday-morning traffic: Bot C2 beaconing is
*low-volume by design* (median 6 bytes vs 70 for benign traffic), so an
outlier detector built on volume is structurally blind to it — no amount of
threshold tuning fixes a detector looking in the opposite direction from
where the attack lives. `docs/DETECTION_STUDY.md` measured that the SAME
features, fed to a supervised RandomForest instead, get AUC 0.9994 / F1
0.9872: the discriminative signal is fully present in the data, it just
needs a paradigm that can learn "ordinary but different" rather than one
that hunts for "rare or extreme".

This module adds that second channel: `SupervisedFlowDetector`, a
`BaseDetector`-conformant (`src/detectors/base.py`, contract C3) wrapper
around `sklearn.ensemble.RandomForestClassifier(class_weight="balanced")`.

Deliberately NOT registered in the global C3 registry
----------------------------------------------------------------------------
An earlier revision of this module called the public
`detectors.registry.register_detector()` at import time, exactly like
`deception.tripwire.TripwireDetector` does. That was wrong, and review
caught it (docs/PHASE5_STATE.md Ticket #5 HIGH-1): `detectors.registry`'s
contract is "registered => benchmarked" — `evaluation.run_evaluation()`
(`src/evaluation/__init__.py`, frozen, Invariant A) iterates every
registered non-tripwire detector and fits it on a BENIGN-ONLY,
label-free split, then publishes precision/recall/F1 for every entry into
the Research Console's benchmark table. A supervised classifier is not a
member of that unsupervised volumetric benchmark family — global
registration was *semantically wrong*, not merely inconvenient, and it
produced a real garbage row (`Supervised_Flow  0.000  0.000  0.000  0.500
0`) in a table that is the project's credibility centrepiece. Note the
precedent this module should have followed from the start:
`src/evaluation/__init__.py` already excludes `tripwire` from that same
loop via `_BENCHMARK_EXCLUDED_DETECTORS`, for the identical documented
reason (a detector whose feature space doesn't match the benchmark "would
trivially predict 'normal' for every row — a meaningless, degenerate
result by construction, not a real one"). `supervised_flow` needed the
same treatment, but adding it to `_BENCHMARK_EXCLUDED_DETECTORS` requires
editing `src/`, which Invariant A forbids — so instead this detector is
simply never handed to the registry at all.

Instead, `SupervisedFlowDetector` is exposed only through the
backend-local `BACKEND_DETECTORS` dict below (and by direct import —
Ticket #7 is the only consumer, so no elaborate lookup indirection is
needed). See docs/PHASE5_STATE.md K6 for the recorded known-issue entry
warning future contributors off re-adding this to the global registry.

Honest scope, per docs/DETECTION_STUDY.md section 4
----------------------------------------------------------------------------
Supervised detection is excellent on threats it has SEEN and blind to
everything else: the study's cross-day, novel-attack-family test (train on
Tuesday+Wednesday brute-force/DoS, test on friday-morning Bot) scored
precision 0.000 / recall 0.000. This is the exact complement of the
volumetric channel's weakness, not a strictly-better replacement for it —
see `backend.streaming.StreamingScorer`'s module docstring for the
"two-channel" framing and `docs/PHASE5_STATE.md` P5-14.

`temporal_split_evaluate()` below reproduces the study's Test 1 (train on
the earlier portion of a day, test on the later portion — a "known threat,
deployed forward in time" scenario, NOT a novel-attack-family scenario) and
is the ONLY evaluation helper this module offers. It deliberately does not
offer a same-distribution self-test entry point: the study's own audit
history is explicit that same-distribution self-test numbers are the
overfitting trap this project has been burned by before, and reporting them
here would repeat it.

Why `fit(X)` (no labels) now raises
----------------------------------------------------------------------------
An earlier revision widened `BaseDetector.fit(self, X)` to `fit(self, X,
y=None)` and, when called without labels, silently fell back to a
trivial always-"normal" state instead of raising — reasoning that
`evaluation.run_evaluation()` fits every registered detector label-free on
a benign-only split, so raising there would break an unrelated frozen
test. That reasoning no longer applies: this detector is not registered
into `detectors.registry.DETECTORS` at all (see above), so
`run_evaluation()` never calls `fit()` on it, label-free or otherwise.
With the registration path gone, a silent "looks fitted, reports nothing"
fallback is not a safety net — it is a trap for the next caller who
passes `X` without `y` (an easy mistake) and gets a detector that runs
without error and flags nothing, rather than a clear signal that it was
used incorrectly. `fit(X, y=None)` therefore raises `ValueError` naming
`fit(X, y)` and `temporal_split_evaluate()` as the correct entry points.
`predict()`/`score_samples()` likewise raise if called before a real
`fit(X, y)` — there is no unfitted-but-usable state.
"""
from __future__ import annotations

import logging
import platform
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.exceptions import InconsistentVersionWarning
from sklearn.metrics import f1_score, precision_score, recall_score, roc_auc_score

from backend.config import BACKEND_SETTINGS
from backend.replay_reader import ReplayFlow, ReplayFlowReader
from detectors.base import BaseDetector
from settings import SETTINGS

logger = logging.getLogger(__name__)

#: The study's top supervised features (Bwd Packet Length Mean,
#: Init_Win_bytes_backward, Init_Win_bytes_forward, Average Packet Size —
#: docs/DETECTION_STUDY.md section 3, feature importances 0.269/0.170/
#: 0.123/0.115) plus the existing volumetric three
#: (SETTINGS.ml.default_features) that `backend.replay_reader.ReplayFlow`
#: already carried before Ticket #5's additive extension. These are the
#: only columns explicitly named anywhere in the study or the ticket
#: brief; no broader "78-feature" set is specified, so this 7-column set
#: is the literal, defensible reading of "the study's top supervised
#: features ... plus the existing volumetric three and the other columns
#: the study used" — see the Ticket #5 report for this judgment call.
SUPERVISED_FEATURE_NAMES: tuple[str, ...] = (
    "duration_sec",
    "packets",
    "bytes",
    "bwd_packet_length_mean",
    "init_win_bytes_forward",
    "init_win_bytes_backward",
    "average_packet_size",
)

#: Name this detector is registered under in detectors.registry.DETECTORS.
DETECTOR_NAME = "supervised_flow"

#: y-label convention used throughout this module: 1 = attack/anomaly,
#: 0 = benign. Matches ReplayFlow.is_attack's boolean sense directly.
_ATTACK_LABEL = 1
_BENIGN_LABEL = 0


def _flows_to_matrix(
    flows: list[ReplayFlow], feature_names: Sequence[str] = SUPERVISED_FEATURE_NAMES
) -> np.ndarray:
    """Build an (n, len(feature_names)) float matrix from flows, column
    order driven by `feature_names` (declared once, not hardcoded
    positionally) — mirrors StreamingScorer._to_frame's same discipline.
    Defaults to the module-level SUPERVISED_FEATURE_NAMES for
    `temporal_split_evaluate()`'s free-function call sites;
    `SupervisedFlowScorer` passes its own `self.feature_names` explicitly
    so a caller that constructs one with a custom feature set is honored,
    not silently ignored."""
    return np.array(
        [[getattr(f, name) for name in feature_names] for f in flows],
        dtype=float,
    )


class SupervisedFlowDetector(BaseDetector):
    """BaseDetector-conformant wrapper around
    `RandomForestClassifier(class_weight="balanced")` — the KNOWN-THREAT
    channel (docs/DETECTION_STUDY.md). NOT registered in
    `detectors.registry.DETECTORS` (see the module docstring); reachable
    only via `BACKEND_DETECTORS` or direct import. See the module
    docstring for why `fit(X, y=None)` raises rather than degrading
    silently, and for this detector's honest scope (strong on threats it
    has seen, zero on novel attack families).

    Sign convention (matches every other AEGIS detector,
    `src/detectors/base.py` / `src/detectors/sklearn_wrappers.py`):
    `predict()` -> -1 for anomaly (attack), 1 for normal;
    `score_samples()` -> lower = more anomalous, derived as
    `-P(attack)` from `predict_proba` so a confident attack call is a
    large negative number, symmetric with IsolationForest's
    `decision_function` convention.
    """

    def __init__(
        self,
        n_estimators: int | None = None,
        random_state: int | None = None,
        class_weight: str | dict | None = "balanced",
    ) -> None:
        # Reuse the project's existing tuning values rather than invent new
        # magic numbers: n_estimators/random_state default to the same
        # SETTINGS.ml values the (unrelated) IsolationForest channel uses,
        # for reproducibility consistency across the codebase, not because
        # they are algorithmically special to a RandomForestClassifier.
        self.n_estimators = (
            n_estimators if n_estimators is not None else SETTINGS.ml.isolation_forest_n_estimators
        )
        self.random_state = (
            random_state if random_state is not None else SETTINGS.ml.isolation_forest_random_state
        )
        self.class_weight = class_weight
        self._clf: RandomForestClassifier | None = None
        self._n_features: int | None = None

    @property
    def is_fitted(self) -> bool:
        """Mirrors `StreamingScorer.is_fitted` (Phase B improvement pass,
        added for `SupervisedFlowScorer.load()`'s artifact validation) —
        public rather than reaching into `_clf` from another class."""
        return self._clf is not None

    def _require_fitted(self) -> None:
        if self._clf is None:
            raise RuntimeError(
                "SupervisedFlowDetector is not fitted. Call fit(X, y) "
                "with real labels first -- there is no unfitted-but-usable "
                "fallback state (see the module docstring: an earlier, "
                "silently-degrading fallback was removed as a review "
                "finding, Ticket #5 HIGH-1)."
            )

    def fit(self, X: np.ndarray, y: np.ndarray | None = None) -> "SupervisedFlowDetector":
        """Fit on (X, y). `y` is required: this detector is deliberately
        NOT registered in `detectors.registry.DETECTORS` (see the module
        docstring), so there is no legitimate label-free caller left to
        accommodate. Raises `ValueError` if `y` is omitted, naming the
        correct entry points, rather than silently returning an
        always-"normal" model."""
        if y is None:
            raise ValueError(
                "SupervisedFlowDetector.fit() requires labels (y=None was "
                "passed). This detector is not part of the unsupervised "
                "detectors.registry.DETECTORS benchmark family and has no "
                "label-free fallback. Use fit(X, y) directly with real "
                "labels, or backend.supervised_detector.temporal_split_"
                "evaluate() for a full honest train/test evaluation."
            )
        X = np.asarray(X, dtype=float)
        self._n_features = X.shape[1] if X.ndim == 2 else 1
        y = np.asarray(y)
        clf = RandomForestClassifier(
            n_estimators=self.n_estimators,
            class_weight=self.class_weight,
            random_state=self.random_state,
        )
        clf.fit(X, y)
        self._clf = clf
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        self._require_fitted()
        X = np.asarray(X, dtype=float)
        raw = self._clf.predict(X)
        return np.where(raw == _ATTACK_LABEL, -1, 1)

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        self._require_fitted()
        X = np.asarray(X, dtype=float)
        proba = self._clf.predict_proba(X)
        classes = list(self._clf.classes_)
        if _ATTACK_LABEL in classes:
            p_attack = proba[:, classes.index(_ATTACK_LABEL)]
        else:
            # Every training row was benign (e.g. a degenerate temporal
            # split) -- the classifier never saw the attack class, so it
            # cannot assign it any probability mass. 0.0 is the honest
            # value, not a crash.
            p_attack = np.zeros(len(X), dtype=float)
        return -p_attack


#: Backend-local name -> class map, DELIBERATELY separate from
#: `detectors.registry.DETECTORS` (see the module docstring and
#: docs/PHASE5_STATE.md K6). `SupervisedFlowDetector` must never be handed
#: to `register_detector()` — `detectors.registry`'s contract is
#: "registered => benchmarked" by `evaluation.run_evaluation()`'s
#: benign-only, label-free loop, and a supervised classifier is not a
#: member of that unsupervised volumetric benchmark family. Ticket #7 is
#: the only intended consumer, and a direct `from
#: backend.supervised_detector import SupervisedFlowDetector` works just
#: as well as looking it up here — this dict exists for symmetry with the
#: `registry.DETECTORS` name -> class pattern, not because indirection is
#: required.
BACKEND_DETECTORS: dict[str, type] = {DETECTOR_NAME: SupervisedFlowDetector}


# ---------------------------------------------------------------------------
# Honest evaluation — temporal split only, never same-distribution self-test
# ---------------------------------------------------------------------------


def temporal_split_evaluate(
    day: str | None = None,
    split_fraction: float = 0.5,
    n_estimators: int | None = None,
    random_state: int | None = None,
) -> dict:
    """Train on the earlier portion of `day`, test on the later portion —
    reproduces docs/DETECTION_STUDY.md's Test 1 (AUC 0.847, precision
    0.9979, recall 0.585). NEVER trains and tests on the same rows: that
    same-distribution self-test is exactly the overfitting trap
    docs/DETECTION_STUDY.md section 4 and this project's prior audit
    history warn against.

    `day` defaults to `BACKEND_SETTINGS.replay_default_dataset_day`
    ("friday-morning" — the only day in this capture with a realistic,
    non-degenerate attack mix for a temporal split; see P5-8).
    `split_fraction` is 0.5 by construction (matching the study's own
    Test 1 methodology, "train on the first half ... test on the second
    half") — a function parameter rather than a BACKEND_SETTINGS field,
    since it is an evaluation-methodology choice, not a deployment
    tunable.

    Raises `datasets.loader.DatasetNotAvailable` (via `ReplayFlowReader`)
    if `datasets/TrafficLabelling ` is not present on disk — callers
    should `pytest.skip()` on it, matching the repo's existing
    graceful-degradation test convention.

    Returns a dict of {day, split_fraction, n_train, n_test,
    n_train_attacks, n_test_attacks, auc, precision, recall, f1,
    feature_names, method}. `method` explicitly names this as a temporal
    (not same-distribution) split so a caller cannot mistake these numbers
    for a self-test.
    """
    resolved_day = day if day is not None else BACKEND_SETTINGS.replay_default_dataset_day
    resolved_n_estimators = (
        n_estimators if n_estimators is not None else SETTINGS.ml.isolation_forest_n_estimators
    )
    resolved_random_state = (
        random_state if random_state is not None else SETTINGS.ml.isolation_forest_random_state
    )

    reader = ReplayFlowReader()
    # iter_flows() already yields chronologically sorted flows (P5-7), so
    # a positional split IS a temporal split with no extra sorting needed.
    flows = list(reader.iter_flows(day=resolved_day))
    n = len(flows)
    split_idx = int(n * split_fraction)
    train_flows = flows[:split_idx]
    test_flows = flows[split_idx:]

    X_train = _flows_to_matrix(train_flows)
    y_train = np.array([_ATTACK_LABEL if f.is_attack else _BENIGN_LABEL for f in train_flows])
    X_test = _flows_to_matrix(test_flows)
    y_test = np.array([_ATTACK_LABEL if f.is_attack else _BENIGN_LABEL for f in test_flows])

    detector = SupervisedFlowDetector(
        n_estimators=resolved_n_estimators, random_state=resolved_random_state
    )
    detector.fit(X_train, y_train)

    preds = detector.predict(X_test)
    scores = detector.score_samples(X_test)  # lower = more anomalous = -P(attack)
    p_attack = -scores
    y_pred_attack = (preds == -1).astype(int)

    try:
        auc = float(roc_auc_score(y_test, p_attack))
    except ValueError:
        auc = float("nan")

    return {
        "day": resolved_day,
        "split_fraction": split_fraction,
        "n_train": len(train_flows),
        "n_test": len(test_flows),
        "n_train_attacks": int(y_train.sum()),
        "n_test_attacks": int(y_test.sum()),
        "auc": auc,
        "precision": float(precision_score(y_test, y_pred_attack, zero_division=0)),
        "recall": float(recall_score(y_test, y_pred_attack, zero_division=0)),
        "f1": float(f1_score(y_test, y_pred_attack, zero_division=0)),
        "feature_names": list(SUPERVISED_FEATURE_NAMES),
        "method": (
            "temporal_split (train=earlier portion, test=later portion of "
            "one day) -- NEVER same-distribution self-test; see "
            "docs/DETECTION_STUDY.md Test 1."
        ),
    }


# ---------------------------------------------------------------------------
# SupervisedFlowScorer — fit-once persistence + live batch scoring
# (Phase B improvement pass: wires the KNOWN-THREAT channel into the live
# Operations Console as a genuine third detector, alongside the volumetric
# and tripwire channels already scored per batch in backend/ingest.py)
# ---------------------------------------------------------------------------


class SupervisedFlowScorerError(RuntimeError):
    """Base class for SupervisedFlowScorer usage/artifact errors."""


class SupervisedFlowScorerNotFitted(SupervisedFlowScorerError):
    """Raised by score_batch/save/baseline on an unfitted scorer."""


class SupervisedFlowScorerArtifactMissing(SupervisedFlowScorerError, FileNotFoundError):
    """Raised by load() when the artifact file does not exist. Unlike
    StreamingScorerArtifactMissing, a caller (backend.runtime.build_runtime)
    is expected to catch this and continue WITHOUT the channel — see this
    class's own module-level role: the known-threat channel is additive,
    not required for the API to serve traffic."""


class SupervisedFlowScorerIncompatible(SupervisedFlowScorerError):
    """Raised by load() on artifact-schema, feature-name, or
    n_features_in_ mismatch. Hard failure, no fallback — mirrors
    StreamingScorerIncompatible's reasoning exactly: a silently-wrong
    scorer is worse than one that refuses to load."""


#: Bump only on a genuine, incompatible artifact-format change.
_ARTIFACT_SCHEMA_VERSION = "1.0"

_ARTIFACT_REQUIRED_KEYS = {
    "artifact_schema_version",
    "detector",
    "feature_names",
    "warmup",
    "hyperparameters",
    "library_versions",
}


@dataclass(frozen=True)
class SupervisedScoredFlow:
    """One flow's known-threat verdict — the RandomForest analogue of
    `backend.streaming.ScoredFlow`. `raw_score` follows this project's
    universal sign convention (lower = more anomalous): it is
    `SupervisedFlowDetector.score_samples()`'s `-P(attack)` directly.
    `calibrated_score` is `P(attack)` itself — unlike the volumetric
    channel's hand-picked-slope sigmoid (`ml_engine.py`'s
    `1/(1+e^(5*raw_score))`), `RandomForestClassifier.predict_proba` is
    already a genuine model-native probability estimate, so no extra
    transform is applied here; "calibrated" means something slightly
    stronger for this channel than for the volumetric one, and that
    difference is worth being able to explain if asked.
    """

    flow: ReplayFlow
    raw_score: float
    calibrated_score: float
    is_anomaly: bool


def _library_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scikit-learn": sklearn.__version__,
        "joblib": joblib.__version__,
    }


class SupervisedFlowScorer:
    """Fit-once persistence + batch scoring wrapper around
    `SupervisedFlowDetector`, mirroring `backend.streaming.StreamingScorer`'s
    contract as closely as the two channels' fundamentally different
    training requirements allow: fit exactly once at build time, persist
    to a joblib artifact, `predict`/`score_samples`-only (never refit) at
    request time.

    Training data, and why this is not a same-distribution self-test
    --------------------------------------------------------------------
    `fit_from_warmup()` trains on the FIRST
    `BACKEND_SETTINGS.supervised_train_split_fraction` (0.5 by default) of
    `BACKEND_SETTINGS.replay_default_dataset_day` ("friday-morning") —
    this is not a methodology invented for deployment. It is EXACTLY
    `temporal_split_evaluate()`'s own train split: the same one
    `docs/DETECTION_STUDY.md` Test 1 already measures and publishes (AUC
    0.847, precision 0.996, recall 0.595 on the held-out SECOND half).

    Because `ReplayEngine` always restarts a day's replay from position
    0, the live channel will show high-confidence, high-accuracy verdicts
    on roughly the FIRST half of any friday-morning demo replay — it has
    genuinely seen those exact labelled rows during training. That is not
    memorization hidden from the record; it is the published methodology,
    deployed as published, and the honestly-measured 0.847 AUC / 0.996
    precision / 0.595 recall numbers describe its performance on the
    SECOND half specifically — the part it has not seen. State this
    plainly if asked "has this model seen this exact demo before": yes,
    for roughly the first half, by design, matching Test 1.

    Unlike `StreamingScorer`, a missing/incompatible artifact here is
    NOT fatal to the API — see `SupervisedFlowScorerArtifactMissing` and
    `backend.runtime.build_runtime()`. This channel is additive.
    """

    def __init__(self, feature_names: Optional[list[str]] = None) -> None:
        self.feature_names: list[str] = (
            list(feature_names) if feature_names is not None else list(SUPERVISED_FEATURE_NAMES)
        )
        self._detector: Optional[SupervisedFlowDetector] = None
        self._warmup_meta: Optional[dict] = None
        self._hyperparameters: Optional[dict] = None
        self._library_versions: Optional[dict] = None

    @property
    def is_fitted(self) -> bool:
        return self._detector is not None

    def _require_fitted(self) -> None:
        if not self.is_fitted:
            raise SupervisedFlowScorerNotFitted(
                "SupervisedFlowScorer is not fitted. Call fit_from_warmup() "
                "(build time) or load() (an existing artifact) first."
            )

    @property
    def baseline(self) -> dict:
        self._require_fitted()
        return {
            "feature_names": list(self.feature_names),
            "warmup": dict(self._warmup_meta),
            "hyperparameters": dict(self._hyperparameters),
            "library_versions": dict(self._library_versions),
        }

    # ------------------------------------------------------------------
    # Fitting (build time only)
    # ------------------------------------------------------------------

    def fit_from_warmup(
        self,
        flows: Optional[Sequence[ReplayFlow]] = None,
        day: Optional[str] = None,
        split_fraction: Optional[float] = None,
        n_estimators: Optional[int] = None,
        random_state: Optional[int] = None,
    ) -> "SupervisedFlowScorer":
        """Fit the RandomForest ONCE on the first `split_fraction` of
        `day`, chronologically. See the class docstring for why this
        specific split (not the whole day, not a different day) is the
        honest, already-published choice.

        `flows` may be supplied directly (tests); otherwise `day` is read
        via `ReplayFlowReader` (defaults to
        `BACKEND_SETTINGS.replay_default_dataset_day`). Raises
        `SupervisedFlowScorerError` if the training slice contains no
        attack rows at all — a classifier trained on zero examples of the
        positive class cannot be meaningfully called a known-threat
        detector (mirrors `StreamingScorer.fit_from_warmup()`'s own
        "refuse a degenerate warmup slice" posture, inverted: here it is
        an ALL-benign slice that is invalid, not an any-attack one).
        """
        resolved_day = day if day is not None else BACKEND_SETTINGS.replay_default_dataset_day
        resolved_split = (
            split_fraction if split_fraction is not None else BACKEND_SETTINGS.supervised_train_split_fraction
        )
        resolved_n_estimators = (
            n_estimators if n_estimators is not None else SETTINGS.ml.isolation_forest_n_estimators
        )
        resolved_random_state = (
            random_state if random_state is not None else SETTINGS.ml.isolation_forest_random_state
        )

        source_file = None
        if flows is None:
            reader = ReplayFlowReader()
            all_flows = list(reader.iter_flows(day=resolved_day))
            if reader.last_read_stats is not None:
                source_file = reader.last_read_stats.source_file
        else:
            all_flows = list(flows)

        split_idx = int(len(all_flows) * resolved_split)
        train_flows = all_flows[:split_idx]

        n_rows = len(train_flows)
        attack_rows = sum(1 for f in train_flows if f.is_attack)
        if attack_rows == 0:
            raise SupervisedFlowScorerError(
                f"Training slice for day={resolved_day!r} split_fraction="
                f"{resolved_split} has {n_rows} row(s) and ZERO attack "
                "rows -- a known-threat detector cannot be fit with no "
                "examples of the class it exists to catch. Pick a day/"
                "split that includes real attack traffic in its first "
                "portion."
            )

        X_train = _flows_to_matrix(train_flows, feature_names=self.feature_names)
        y_train = np.array([_ATTACK_LABEL if f.is_attack else _BENIGN_LABEL for f in train_flows])

        detector = SupervisedFlowDetector(
            n_estimators=resolved_n_estimators, random_state=resolved_random_state
        )
        detector.fit(X_train, y_train)

        ts_values = [f.ts for f in train_flows]
        self._detector = detector
        self._warmup_meta = {
            "day": resolved_day,
            "source_file": source_file,
            "source_dataset": train_flows[0].source_dataset,
            "split_fraction": resolved_split,
            "rows_used": n_rows,
            "attack_rows_in_training": attack_rows,
            "ts_min": min(ts_values).isoformat(),
            "ts_max": max(ts_values).isoformat(),
            "fitted_at": datetime.now(timezone.utc).isoformat(),
        }
        self._hyperparameters = {
            "n_estimators": resolved_n_estimators,
            "random_state": resolved_random_state,
            "class_weight": "balanced",
        }
        self._library_versions = _library_versions()
        return self

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[Path] = None) -> Path:
        """joblib-dump to `BACKEND_SETTINGS.supervised_model_artifact_path_resolved`
        unless an explicit `path` is given. Creates parent directories.
        Returns the absolute path written."""
        self._require_fitted()
        target = (
            Path(path) if path is not None else BACKEND_SETTINGS.supervised_model_artifact_path_resolved
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        artifact = {
            "artifact_schema_version": _ARTIFACT_SCHEMA_VERSION,
            "detector": self._detector,
            "feature_names": list(self.feature_names),
            "warmup": dict(self._warmup_meta),
            "hyperparameters": dict(self._hyperparameters),
            "library_versions": dict(self._library_versions),
        }
        joblib.dump(artifact, target)
        return target.resolve()

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SupervisedFlowScorer":
        """Load a persisted scorer. Defaults to
        `BACKEND_SETTINGS.supervised_model_artifact_path_resolved`.

        Raises `SupervisedFlowScorerArtifactMissing` if the artifact file
        is absent — callers (`backend.runtime.build_runtime`) are
        expected to catch this and continue with the channel disabled,
        unlike `StreamingScorer.load()`'s fatal-to-the-API failure mode.
        Raises `SupervisedFlowScorerIncompatible` on schema/feature-name/
        `n_features_in_` mismatch, or if `joblib.load` itself raises.
        """
        target = (
            Path(path) if path is not None else BACKEND_SETTINGS.supervised_model_artifact_path_resolved
        )
        if not target.exists():
            raise SupervisedFlowScorerArtifactMissing(
                f"No SupervisedFlowScorer artifact at {target}. Build it "
                "with: PYTHONPATH=src venv/bin/python -m backend.warmup_supervised"
            )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=InconsistentVersionWarning)
            try:
                artifact = joblib.load(target)
            except Exception as exc:  # noqa: BLE001 — any unpickle failure is fatal here
                raise SupervisedFlowScorerIncompatible(
                    f"Failed to load SupervisedFlowScorer artifact at {target}: {exc}"
                ) from exc

        for w in caught:
            if issubclass(w.category, InconsistentVersionWarning):
                logger.warning(
                    "SupervisedFlowScorer artifact at %s was pickled under "
                    "a different scikit-learn version (%s); scores may be "
                    "affected.",
                    target,
                    w.message,
                )

        if not isinstance(artifact, dict):
            raise SupervisedFlowScorerIncompatible(
                f"Artifact at {target} is not a dict (got {type(artifact).__name__})."
            )
        missing_keys = _ARTIFACT_REQUIRED_KEYS - artifact.keys()
        if missing_keys:
            raise SupervisedFlowScorerIncompatible(
                f"Artifact at {target} is missing key(s) {sorted(missing_keys)}."
            )
        if artifact["artifact_schema_version"] != _ARTIFACT_SCHEMA_VERSION:
            raise SupervisedFlowScorerIncompatible(
                f"Artifact schema version {artifact['artifact_schema_version']!r} "
                f"at {target} != expected {_ARTIFACT_SCHEMA_VERSION!r}."
            )
        if list(artifact["feature_names"]) != list(SUPERVISED_FEATURE_NAMES):
            raise SupervisedFlowScorerIncompatible(
                f"Artifact feature_names {artifact['feature_names']!r} at "
                f"{target} != SUPERVISED_FEATURE_NAMES "
                f"{list(SUPERVISED_FEATURE_NAMES)!r}."
            )

        detector = artifact["detector"]
        if not isinstance(detector, SupervisedFlowDetector) or not detector.is_fitted:
            raise SupervisedFlowScorerIncompatible(
                f"Artifact at {target} does not contain a fitted "
                "SupervisedFlowDetector."
            )
        expected_n = len(artifact["feature_names"])
        if detector._n_features != expected_n:
            raise SupervisedFlowScorerIncompatible(
                f"Artifact at {target} has n_features mismatch: "
                f"detector={detector._n_features}, expected {expected_n}."
            )

        scorer = cls(feature_names=artifact["feature_names"])
        scorer._detector = detector
        scorer._warmup_meta = dict(artifact["warmup"])
        scorer._hyperparameters = dict(artifact["hyperparameters"])
        scorer._library_versions = dict(artifact["library_versions"])
        return scorer

    # ------------------------------------------------------------------
    # Scoring (hot path)
    # ------------------------------------------------------------------

    def score_batch(self, flows: Sequence[ReplayFlow]) -> list[SupervisedScoredFlow]:
        """Score a micro-batch. `predict`/`score_samples` only, never
        `fit`. Empty input returns `[]`."""
        self._require_fitted()
        if not flows:
            return []
        flows = list(flows)
        X = _flows_to_matrix(flows, feature_names=self.feature_names)
        preds = self._detector.predict(X)
        raw_scores = self._detector.score_samples(X)  # -P(attack)
        return [
            SupervisedScoredFlow(
                flow=flow,
                raw_score=float(raw_scores[i]),
                calibrated_score=float(-raw_scores[i]),  # P(attack), native
                is_anomaly=bool(preds[i] == -1),
            )
            for i, flow in enumerate(flows)
        ]

    def score_event(self, flow: ReplayFlow) -> SupervisedScoredFlow:
        """Score one flow. CONVENIENCE/TEST API ONLY — see
        `StreamingScorer.score_event`'s identical caveat; callers on the
        hot path must call `score_batch()`."""
        return self.score_batch([flow])[0]
