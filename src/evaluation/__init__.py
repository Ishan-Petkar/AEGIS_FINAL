"""
evaluation — Evaluation harness for AEGIS Phase 3.

Closes the biggest credibility gaps in the original Phase 0 harness:
running our anomaly detectors against real, labeled ground truth and
reporting honest Precision/Recall/F1/AUC, refusing to report metrics
computed on a degenerate (near-all-benign or near-all-anomaly) eval split,
and scoring time-series ICS data (SWaT) with segment-wise metrics instead of
the "point-adjust" convention known to make random noise look state-of-the-art
(see evaluation.metrics's module docstring and research/BENCHMARKS.md B5).

Usage
-----
    # From project root
    PYTHONPATH=src python -m evaluation --dataset cic_ids2017 --limit 20000 --no-ocsvm

    # Or import programmatically
    from evaluation import run_evaluation
    results = run_evaluation(limit=20_000)
    for r in results:
        print(r)

Design
------
The harness:
  1. Loads the requested dataset via the existing canonical loader
     (datasets.loader.load_dataset), falling back to synthetic data if the
     requested source isn't available on disk.
  2. Derives binary ground-truth labels from the canonical ``action`` column
     (ACTION_ALERT -> anomaly=True).
  3. Trains each registered volumetric-style detector
     (detectors.registry.DETECTORS, excluding "tripwire" — see below) on a
     held-out *benign-only* training split, to simulate unsupervised
     learning (models never see attack labels during training).
  4. Refuses to proceed if the eval split's positive rate falls outside
     [SETTINGS.evaluation.min_positive_rate, SETTINGS.evaluation.max_positive_rate]
     — raises DegenerateEvaluationError instead of silently reporting
     P=0.000 R=0.000 F1=0.000 AUC=nan.
  5. Evaluates on the full eval split and reports Precision, Recall, F1, and
     ROC-AUC — point-wise for flow/transaction datasets, segment-wise
     (evaluation.metrics) for time-series ICS data (SWaT).
  6. Returns a list of EvalResult dataclasses for downstream use (UI panel,
     tests).

Detector registry, and why "tripwire" is excluded here
--------------------------------------------------------
run_evaluation() iterates detectors.registry.DETECTORS instead of hardcoding
each detector's fit/predict calls (contract C3's whole point — a new
detector is benchmarked the moment it's registered). TripwireDetector is
deliberately excluded from this loop: its only feature is
``is_honeytoken_use``, a column that does not exist on ordinary
CIC-IDS2017/PaySim/SWaT traffic, so forced through this pipeline it would
trivially predict "normal" for every row — a meaningless, degenerate result
by construction, not a real one. Tripwire's own metric is lead time
(evaluation.lead_time), measured on the scripted-attack recon/exfil timeline
it actually operates on, not on this precision/recall/F1 loop.

Label convention: anomaly=1, normal=0 (sklearn binary classification).
"""
from __future__ import annotations

import pathlib
import sys
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from sklearn.metrics import f1_score, precision_score, recall_score, roc_auc_score

# Ensure src/ is on the path when run directly / via -m.
_SRC = pathlib.Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from detectors.registry import DETECTORS  # noqa: E402
from evaluation.metrics import segment_wise_precision_recall  # noqa: E402
from settings import SETTINGS  # noqa: E402

#: Detectors that don't belong in the standard volumetric benchmark loop.
#: See the module docstring for why "tripwire" is excluded.
_BENCHMARK_EXCLUDED_DETECTORS = {"tripwire"}

#: Human-readable display names for registry keys (cosmetic only).
_DISPLAY_NAMES = {
    "isolation_forest": "Isolation Forest",
    "zscore": "Z-Score (baseline)",
    "mad": "MAD (baseline)",
    "ocsvm": "One-Class SVM (baseline)",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DegenerateEvaluationError(Exception):
    """Raised when an eval split's positive rate is too extreme to be meaningful.

    Replaces the old silent failure mode: a (near-)100%-benign or
    (near-)100%-anomaly split used to produce P=0.000 R=0.000 F1=0.000
    AUC=nan without any signal that the *split*, not the detector, was the
    problem. See SETTINGS.evaluation.min_positive_rate / max_positive_rate.
    """


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    """Evaluation results for a single detector."""

    detector: str          # Human-readable detector name
    precision: float
    recall: float
    f1: float
    roc_auc: float
    n_total: int           # Total samples evaluated
    n_true_anomalies: int  # Ground-truth positives
    n_predicted_anomalies: int  # Model-predicted positives
    dataset: str = "CIC-IDS2017"
    scoring: str = "pointwise"  # "pointwise" or "segment-wise" (see evaluation.metrics)

    def __str__(self) -> str:
        return (
            f"[{self.detector}] "
            f"P={self.precision:.3f}  R={self.recall:.3f}  "
            f"F1={self.f1:.3f}  AUC={self.roc_auc:.3f}  "
            f"(pred {self.n_predicted_anomalies}/{self.n_true_anomalies} anomalies "
            f"from {self.n_total} samples, {self.scoring})"
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core evaluation function
# ---------------------------------------------------------------------------

def run_evaluation(
    dataset: str = "cic_ids2017",
    limit: int = 20_000,
    train_fraction: float = 0.5,
    random_state: int = 42,
    include_ocsvm: bool = True,
    verbose: bool = True,
    min_positive_rate: float | None = None,
    max_positive_rate: float | None = None,
) -> list[EvalResult]:
    """Run all registered (non-tripwire) detectors against ground truth.

    Parameters
    ----------
    dataset:
        Dataset name accepted by datasets.loader.load_dataset (e.g.
        "cic_ids2017", "paysim", "swat", "synthetic", "deception").
    limit:
        Maximum rows to load.
    train_fraction:
        Fraction of *benign-only* rows used for unsupervised training.
        Remaining rows (benign + all attacks) form the evaluation set.
    random_state:
        Seed for reproducibility.
    include_ocsvm:
        Whether to include the registry's "ocsvm" detector (slowest, skip
        for fast runs).
    verbose:
        Print results to stdout.
    min_positive_rate, max_positive_rate:
        Override SETTINGS.evaluation.min_positive_rate / max_positive_rate
        for this call (optional-override convention).

    Returns
    -------
    List of EvalResult, one per benchmarked detector.

    Raises
    ------
    DegenerateEvaluationError
        If the eval split's positive rate falls outside
        [min_positive_rate, max_positive_rate].
    """
    # ------------------------------------------------------------------
    # 1. Load data
    # ------------------------------------------------------------------
    from datasets.loader import DatasetNotAvailable, load_dataset

    try:
        batch = load_dataset(dataset, limit=limit)
        dataset_name = dataset.upper()
    except DatasetNotAvailable as exc:
        if verbose:
            print(f"[evaluation] {dataset} not available: {exc}")
            print("[evaluation] Falling back to synthetic data (ground truth = is_anomaly flag).")
        batch = load_dataset("synthetic", limit=limit)
        dataset_name = "synthetic"

    df = batch.df

    # ------------------------------------------------------------------
    # 2. Derive ground-truth labels
    # ------------------------------------------------------------------
    # ACTION_ALERT events are labeled anomalies by the dataset adapter.
    from datasets.schema import ACTION_ALERT

    y_true = (df["action"] == ACTION_ALERT).astype(int).values  # 1 = anomaly

    # ------------------------------------------------------------------
    # 3. Extract features
    # ------------------------------------------------------------------
    from datasets.schema import CANONICAL_COLUMNS
    from ml_engine import preprocess_features

    is_swat = dataset_name.lower() == "swat"
    if is_swat:
        feature_cols = [
            c for c in df.columns
            if c not in CANONICAL_COLUMNS and pd.api.types.is_numeric_dtype(df[c])
        ]
    else:
        feature_cols = list(SETTINGS.ml.default_features)
        # Ensure numeric columns exist — synthetic/deception fallback may lack some
        for col in feature_cols:
            if col not in df.columns:
                df[col] = 0.0

    X_full, _ = preprocess_features(df, features=feature_cols)

    # ------------------------------------------------------------------
    # 4. Train/eval split — train on benign-only subset
    # ------------------------------------------------------------------
    benign_idx = np.where(y_true == 0)[0]
    rng = np.random.default_rng(random_state)
    rng.shuffle(benign_idx)
    n_train = int(len(benign_idx) * train_fraction)
    train_idx = benign_idx[:n_train]
    X_train = X_full[train_idx]

    # Evaluation covers the entire dataset (all attacks + remaining benign).
    # Kept in original chronological order (not shuffled) — point-wise
    # metrics are order-invariant, but segment-wise scoring (SWaT) requires
    # contiguity to mean anything, so this must never be shuffled.
    eval_idx = np.sort(np.concatenate([benign_idx[n_train:], np.where(y_true == 1)[0]]))
    X_eval = X_full[eval_idx]
    y_eval = y_true[eval_idx]

    # ------------------------------------------------------------------
    # 4b. Degenerate-split guard — replaces the old silent
    #     P=0.000 R=0.000 F1=0.000 AUC=nan failure mode.
    # ------------------------------------------------------------------
    lo = min_positive_rate if min_positive_rate is not None else SETTINGS.evaluation.min_positive_rate
    hi = max_positive_rate if max_positive_rate is not None else SETTINGS.evaluation.max_positive_rate
    pos_rate = float(y_eval.mean()) if len(y_eval) > 0 else 0.0
    if len(y_eval) == 0 or pos_rate < lo or pos_rate > hi:
        raise DegenerateEvaluationError(
            f"Eval split for dataset={dataset_name!r} (limit={limit}) has a "
            f"positive rate of {pos_rate:.4f} over {len(y_eval)} rows, "
            f"outside the sane [{lo}, {hi}] band "
            f"(SETTINGS.evaluation.min_positive_rate / max_positive_rate). "
            "Refusing to report precision/recall/F1/AUC computed on a "
            "degenerate split — these numbers would be meaningless "
            "(a detector predicting 'normal' for everything would score "
            "P=R=F1=0 here, not because it failed, but because the split "
            "gave it nothing to discriminate). Widen the eval slice "
            "(increase --limit), pick a dataset with more balanced ground "
            "truth, or adjust the min/max_positive_rate bounds if this "
            "split is intentional."
        )

    if verbose:
        print(f"[evaluation] Train: {len(X_train)} benign rows | "
              f"Eval: {len(X_eval)} rows ({pos_rate * 100:.1f}% anomalies)")

    # ------------------------------------------------------------------
    # 5. Run each registered (non-tripwire) detector
    # ------------------------------------------------------------------
    results: list[EvalResult] = []

    for name, detector_cls in DETECTORS.items():
        if name in _BENCHMARK_EXCLUDED_DETECTORS:
            continue
        if name == "ocsvm" and not include_ocsvm:
            continue

        display_name = _DISPLAY_NAMES.get(name, name.title())
        if verbose:
            print(f"[evaluation] Training {display_name}...")

        detector = detector_cls()
        detector.fit(X_train)
        preds = detector.predict(X_eval)
        scores = detector.score_samples(X_eval)

        if is_swat:
            result = _evaluate_segment_wise(display_name, preds, scores, y_eval, dataset=dataset_name)
        else:
            result = _evaluate(display_name, preds, scores, y_eval, dataset=dataset_name)
        results.append(result)

    # ------------------------------------------------------------------
    # 6. Report
    # ------------------------------------------------------------------
    if verbose:
        print("\n" + "=" * 72)
        print("AEGIS Phase 3 — Anomaly Detection Evaluation Results")
        print("=" * 72)
        for r in results:
            print(r)
        print("=" * 72)

    return results


def results_to_dataframe(results: list[EvalResult]) -> pd.DataFrame:
    """Convert a list of EvalResult into a tidy comparison DataFrame."""
    return pd.DataFrame([r.to_dict() for r in results]).set_index("detector")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _evaluate(
    name: str,
    preds: np.ndarray,
    scores: np.ndarray,
    y_true: np.ndarray,
    dataset: str = "CIC-IDS2017",
) -> EvalResult:
    """Compute point-wise metrics for a single detector's predictions."""
    # sklearn uses -1/1 convention; convert to 0/1 for metrics
    y_pred = (preds == -1).astype(int)

    # Guard: if model predicts everything as one class, AUC is undefined
    try:
        auc = roc_auc_score(y_true, -scores)  # higher score = more normal -> negate
    except ValueError:
        auc = float("nan")

    return EvalResult(
        detector=name,
        precision=float(precision_score(y_true, y_pred, zero_division=0)),
        recall=float(recall_score(y_true, y_pred, zero_division=0)),
        f1=float(f1_score(y_true, y_pred, zero_division=0)),
        roc_auc=auc,
        n_total=int(len(y_true)),
        n_true_anomalies=int(y_true.sum()),
        n_predicted_anomalies=int(y_pred.sum()),
        dataset=dataset,
        scoring="pointwise",
    )


def _evaluate_segment_wise(
    name: str,
    preds: np.ndarray,
    scores: np.ndarray,
    y_true: np.ndarray,
    dataset: str = "SWaT",
) -> EvalResult:
    """Compute segment-wise metrics for a single detector (time-series ICS data).

    See evaluation.metrics.segment_wise_precision_recall for the methodology
    and the documented rejection of point-adjust scoring.
    """
    y_pred = (preds == -1).astype(int)
    sw = segment_wise_precision_recall(y_true, y_pred)

    try:
        auc = roc_auc_score(y_true, -scores)
    except ValueError:
        auc = float("nan")

    return EvalResult(
        detector=name,
        precision=sw.precision,
        recall=sw.recall,
        f1=sw.f1,
        roc_auc=auc,
        n_total=int(len(y_true)),
        n_true_anomalies=int(y_true.sum()),
        n_predicted_anomalies=int(y_pred.sum()),
        dataset=dataset,
        scoring="segment-wise",
    )
