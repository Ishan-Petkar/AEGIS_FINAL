"""
pipeline.py — Single analytics entry point for AEGIS (Phase 1, contract C1).

Extracts the data -> preprocess -> train -> score -> CII sequence that used to
live inline in aegis_demo.py. No Streamlit import is permitted in this module —
that is the whole point: run_analysis() must be callable headless (tests, a
future API handler, a stream consumer) with nothing resembling a UI framework
involved. The dashboard is the only caller that renders the result; it does
not perform any of this computation itself.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from cii_calculator import CIIResult, compute_cascading_impact_full
from core.result import AnalysisResult
from data_generator import generate_mock_network_data
from datasets.loader import DatasetNotAvailable, load_dataset
from deception.tripwire import TripwireDetector
from ml_engine import (
    add_asset_names,
    compute_anomaly_scores,
    preprocess_features,
    train_isolation_forest,
)
from settings import SETTINGS


def run_analysis(
    dataset: str = "synthetic",
    num_nodes: Optional[int] = None,
    num_edges: Optional[int] = None,
    anomaly_rate: Optional[float] = None,
    model_estimators: Optional[int] = None,
    attack_edges: Optional[list[dict]] = None,
    active_attack: Optional[str] = None,
    anomalous_asset: Optional[str] = None,
    dataset_limit: int = 5000,
    random_seed: int = 42,
) -> AnalysisResult:
    """
    Run one full analysis pass: load traffic, score anomalies, and — if an
    attack/what-if scenario is active — compute the Monte Carlo cascading
    impact.

    Parameters mirror the dashboard's sidebar controls; all default to None
    and fall back to SETTINGS-driven values inside generate_mock_network_data()
    / ml_engine, per the project's optional-override convention. A headless
    caller (tests, an eventual API) can omit everything except `dataset`.

    Parameters
    ----------
    dataset:
        "synthetic", or a real dataset name accepted by datasets.loader
        (e.g. "cic_ids2017", "paysim", "swat"). Non-synthetic datasets
        replace the generated edges with real (scored) traffic; nodes remain
        the static smart-city asset list either way.
    attack_edges:
        Extra synthetic edges to inject (a scripted attack preset), appended
        to whatever traffic was loaded.
    active_attack, anomalous_asset:
        If both are set, a Monte Carlo CII computation is run against
        anomalous_asset. If not, the result's `cii` field is an empty
        CIIResult (matching the dashboard's "no compromise simulated" state).
    dataset_limit:
        Row cap when loading a real dataset.
    random_seed:
        Seed for both the synthetic generator and the CII simulation.

    Returns
    -------
    AnalysisResult
    """
    nodes_df, edges_df = generate_mock_network_data(
        num_nodes=num_nodes,
        num_edges=num_edges,
        anomaly_rate=anomaly_rate,
        seed=random_seed,
    )

    dataset_warning: Optional[str] = None
    if dataset != "synthetic":
        try:
            canonical_batch = load_dataset(dataset, limit=dataset_limit)
            c_df = canonical_batch.df
            if not c_df.empty:
                # Canonical feature extractor — handles datasets (e.g. SWaT)
                # that don't carry duration_sec/packets/bytes columns directly.
                ml_features = canonical_batch.to_ml_features()
                edges_df = pd.DataFrame({
                    "source": ml_features["source_asset_id"],
                    "target": ml_features["destination_asset_id"],
                    "duration_sec": ml_features["duration_sec"],
                    "packets": ml_features["packets"],
                    "bytes": ml_features["bytes"],
                    "timestamp": c_df["timestamp"].astype(str),
                    "attck_evidence": c_df["attck_evidence"],
                    "provenance": c_df["provenance"],
                })
                if "is_honeytoken_use" in c_df.columns:
                    # Phase 2: the "deception" dataset (and any future source
                    # that reuses deception/adapter.py) carries this column on
                    # the raw canonical frame — to_ml_features() doesn't
                    # surface it (it's not a flow feature), so it must be
                    # copied across explicitly or the tripwire fusion below
                    # would never see it.
                    edges_df["is_honeytoken_use"] = c_df["is_honeytoken_use"].to_numpy()
        except DatasetNotAvailable as exc:
            dataset_warning = str(exc)

    if attack_edges:
        edges_df = pd.concat([edges_df, pd.DataFrame(attack_edges)], ignore_index=True)

    ip_to_asset = {row["ip"]: row["asset_name"] for _, row in nodes_df.dropna(subset=["ip"]).iterrows()}
    criticality_map = {row["asset_name"]: row["criticality"] for _, row in nodes_df.iterrows()}

    X_scaled, _scaler = preprocess_features(edges_df)
    clf = train_isolation_forest(X_scaled, n_estimators=model_estimators, contamination=anomaly_rate)
    edges_df = compute_anomaly_scores(clf, X_scaled, edges_df)
    edges_df = add_asset_names(edges_df, ip_to_asset)

    # ------------------------------------------------------------------
    # Phase 2: fuse the deterministic honeytoken tripwire signal with the
    # volumetric (Isolation Forest) signal. Two independent detectors that
    # both fire on the same connection escalate confidence rather than one
    # replacing the other — see deception/tripwire.py and
    # deception/adapter.py for how `is_honeytoken_use` gets onto edges_df
    # (attack_edges recon rows from data_generator.generate_scripted_attack,
    # or a "deception" dataset load).
    # ------------------------------------------------------------------
    deception_cfg = SETTINGS.deception
    tw_features = TripwireDetector.features_from_df(edges_df)
    tripwire_detector = TripwireDetector().fit(tw_features)
    tripwire_fired = tripwire_detector.predict(tw_features) == -1
    volume_fired = edges_df["is_anomaly"].to_numpy()

    edges_df["tripwire_fired"] = tripwire_fired
    edges_df["is_anomaly"] = volume_fired | tripwire_fired
    edges_df["confidence"] = np.select(
        [
            volume_fired & tripwire_fired,
            tripwire_fired,
            volume_fired,
        ],
        [
            deception_cfg.confidence_both,
            deception_cfg.confidence_tripwire_only,
            deception_cfg.confidence_volume_only,
        ],
        default=deception_cfg.confidence_none,
    )

    src_col = "source_asset" if "source_asset" in edges_df.columns else "source"
    tgt_col = "target_asset" if "target_asset" in edges_df.columns else "target"
    anomalous_ips = set(edges_df[edges_df["is_anomaly"]][src_col]).union(
        set(edges_df[edges_df["is_anomaly"]][tgt_col])
    )
    anomalous_assets = {ip_to_asset.get(ip, ip) for ip in anomalous_ips}

    if active_attack is None and anomalous_asset is None and tripwire_fired.any():
        # A honeytoken fired with no explicit compromise scenario selected
        # (e.g. dataset="deception", or a recon stage alone before its exfil
        # step) — still worth a CII estimate rather than silently reporting
        # zero risk. Target the first tripwire hit's destination (a
        # Purdue-zone gateway node name); cii_calculator's tripwire-only
        # branch handles tracing that to whatever it guards.
        tripwire_targets = edges_df.loc[tripwire_fired, tgt_col]
        anomalous_asset = tripwire_targets.iloc[0]
        active_attack = f"Tripwire: {anomalous_asset}"

    nodes_df = nodes_df.copy()
    nodes_df["status"] = nodes_df["asset_name"].apply(
        lambda x: "Threat Highlighted" if x in anomalous_assets else "Secure"
    )

    total_connections = len(edges_df)
    anomalous_connections = int(edges_df["is_anomaly"].sum())
    anomaly_pct = (anomalous_connections / total_connections) * 100 if total_connections > 0 else 0.0

    if active_attack and anomalous_asset:
        cii = compute_cascading_impact_full(
            anomalous_asset,
            0.98,
            criticality_map=criticality_map,
            random_seed=random_seed,
        )
    else:
        cii = CIIResult()

    if active_attack:
        network_risk = min(100, int(cii.cii_median * 100))
    elif total_connections > 0:
        network_risk = min(100, int((anomalous_connections / total_connections) * 300))
    else:
        network_risk = 0

    return AnalysisResult(
        nodes_df=nodes_df,
        edges_df=edges_df,
        ip_to_asset=ip_to_asset,
        criticality_map=criticality_map,
        total_connections=total_connections,
        anomalous_connections=anomalous_connections,
        anomaly_pct=anomaly_pct,
        cii=cii,
        network_risk=network_risk,
        dataset_warning=dataset_warning,
    )
