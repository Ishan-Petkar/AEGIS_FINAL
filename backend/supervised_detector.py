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

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import f1_score, precision_score, recall_score, roc_auc_score

from backend.config import BACKEND_SETTINGS
from backend.replay_reader import ReplayFlow, ReplayFlowReader
from detectors.base import BaseDetector
from settings import SETTINGS

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


def _flows_to_matrix(flows: list[ReplayFlow]) -> np.ndarray:
    """Build an (n, len(SUPERVISED_FEATURE_NAMES)) float matrix from flows,
    column order driven by SUPERVISED_FEATURE_NAMES (declared once, not
    hardcoded positionally) — mirrors StreamingScorer._to_frame's same
    discipline."""
    return np.array(
        [[getattr(f, name) for name in SUPERVISED_FEATURE_NAMES] for f in flows],
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
