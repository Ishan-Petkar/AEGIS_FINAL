"""
ml_engine.py — Machine learning engine for AEGIS.

Provides data preprocessing, model training, and anomaly scoring.
All hyperparameters are drawn from SETTINGS (settings.py).
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from settings import SETTINGS
from detectors.statistical import MADDetector, ZScoreDetector  # noqa: F401 — re-exported


def preprocess_features(
    edges_df: pd.DataFrame,
    features: list[str] | None = None,
    scaler: StandardScaler | None = None,
) -> tuple[np.ndarray, StandardScaler]:
    """Scale numerical features for anomaly detection.

    Parameters
    ----------
    edges_df:
        DataFrame of network or canonical events.
    features:
        Column names to use. Defaults to SETTINGS.ml.default_features.
        If canonical schema columns are present (`payload_size`), they are mapped automatically.
    scaler:
        Optional pre-fitted StandardScaler. When given, features are
        `.transform()`-ed with it instead of fitting a new one — use this
        to score a held-out split with statistics derived only from the
        training split, avoiding train/eval leakage (a scaler fit on rows
        the eval split will later be graded on lets those rows quietly
        influence the eval split's own normalisation). When None
        (the default), a new StandardScaler is fit via `.fit_transform()`,
        unchanged from every existing caller's behavior.

    Returns
    -------
    X_scaled:
        Scaled numpy array ready for model training/inference.
    scaler:
        The StandardScaler used (newly fit, or the one passed in).
    """
    df = edges_df.copy()

    if features is None:
        features = list(SETTINGS.ml.default_features)

    # Auto-map canonical schema column payload_size -> bytes if bytes is requested but missing
    if "bytes" in features and "bytes" not in df.columns and "payload_size" in df.columns:
        df["bytes"] = df["payload_size"]
    if "duration_sec" in features and "duration_sec" not in df.columns:
        df["duration_sec"] = 1.0
    if "packets" in features and "packets" not in df.columns:
        df["packets"] = 1

    X = df[features].astype(float).copy()
    if scaler is None:
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
    else:
        X_scaled = scaler.transform(X)
    return X_scaled, scaler


def train_isolation_forest(
    X_scaled: np.ndarray,
    n_estimators: int | None = None,
    contamination: float | None = None,
    random_state: int | None = None,
) -> IsolationForest:
    """Train an Isolation Forest on scaled data.

    Parameters default to SETTINGS.ml values, but can be overridden for
    hyperparameter searches without changing global config.

    Returns
    -------
    clf:
        Fitted IsolationForest model.
    """
    clf = IsolationForest(
        n_estimators=n_estimators or SETTINGS.ml.isolation_forest_n_estimators,
        contamination=contamination or SETTINGS.ml.isolation_forest_contamination,
        random_state=random_state if random_state is not None else SETTINGS.ml.isolation_forest_random_state,
    )
    clf.fit(X_scaled)
    return clf


def compute_anomaly_scores(
    clf: IsolationForest,
    X_scaled: np.ndarray,
    edges_df: pd.DataFrame,
) -> pd.DataFrame:
    """Compute anomaly scores and flags, adding columns to edges_df.

    Adds columns:
    - ``anomaly_score``: IsolationForest prediction (-1 = anomaly, 1 = normal).
    - ``raw_score``: decision_function output (more negative → more anomalous).
    - ``calibrated_score``: Calibrated risk probability in [0, 1].
    - ``is_anomaly``: boolean flag (True when anomaly_score == -1).

    Returns the modified DataFrame (also mutates in-place).
    """
    edges_df = edges_df.copy()
    edges_df["anomaly_score"] = clf.predict(X_scaled)
    edges_df["raw_score"] = clf.decision_function(X_scaled)
    edges_df["is_anomaly"] = edges_df["anomaly_score"] == -1

    # Sigmoid calibration: raw_score is <0 for anomalies, >0 for normal.
    # We want anomalies to approach 1.0 and normal to approach 0.0.
    edges_df["calibrated_score"] = 1.0 / (1.0 + np.exp(5.0 * edges_df["raw_score"]))

    return edges_df


def add_asset_names(
    edges_df: pd.DataFrame,
    ip_to_asset: dict[str, str],
) -> pd.DataFrame:
    """Map source/target IPs or asset IDs to human-readable asset names.

    Returns the modified DataFrame.
    """
    edges_df = edges_df.copy()
    src_col = "source_asset_id" if "source_asset_id" in edges_df.columns else "source"
    tgt_col = "destination_asset_id" if "destination_asset_id" in edges_df.columns else "target"

    if src_col in edges_df.columns:
        edges_df["source_asset"] = edges_df[src_col].map(lambda x: ip_to_asset.get(str(x), str(x)))
    if tgt_col in edges_df.columns:
        edges_df["target_asset"] = edges_df[tgt_col].map(lambda x: ip_to_asset.get(str(x), str(x)))
    return edges_df


def model_hyperparameters(
    n_estimators: int | None = None,
    contamination: float | None = None,
    features: list[str] | None = None,
) -> dict:
    """Return a summary dict of active hyperparameters for display/logging."""
    return {
        "algorithm": "Isolation Forest",
        "n_estimators": n_estimators or SETTINGS.ml.isolation_forest_n_estimators,
        "contamination": contamination or SETTINGS.ml.isolation_forest_contamination,
        "features_used": features or list(SETTINGS.ml.default_features),
    }


# ---------------------------------------------------------------------------
# Phase 3 — Baseline detectors for evaluation comparison
# ---------------------------------------------------------------------------
# ZScoreDetector and MADDetector now live in detectors/statistical.py (contract
# C3) and are re-exported above for backward compatibility.


def train_ocsvm_baseline(
    X_scaled: np.ndarray,
    nu: float = 0.08,
    kernel: str = "rbf",
):
    """Train a One-Class SVM baseline for anomaly detection.

    Parameters
    ----------
    X_scaled:
        Pre-scaled feature array.
    nu:
        Upper bound on the fraction of training errors (analogous to contamination).
        Defaults to the same value as IsolationForest contamination.
    kernel:
        SVM kernel. ``'rbf'`` is the standard choice.

    Returns
    -------
    Fitted OneClassSVM model.
    """
    from sklearn.svm import OneClassSVM
    clf = OneClassSVM(nu=nu, kernel=kernel)
    clf.fit(X_scaled)
    return clf

