"""
loader.py — Unified dataset loader interface for AEGIS Phase 1.

Single entry point for any downstream component (ML engine, CII calculator,
Streamlit dashboard) that needs a canonical event batch.  No component should
ever call an adapter directly — all access goes through `load_dataset()`.

Usage
-----
    from datasets.loader import load_dataset

    batch = load_dataset("cic_ids2017", limit=10_000)
    batch = load_dataset("paysim", limit=5_000)
    batch = load_dataset("synthetic")          # falls back to data_generator
    batch = load_dataset("swat")               # raises DatasetNotAvailable
"""
from __future__ import annotations

import pathlib
from typing import Optional

import numpy as np
import pandas as pd

from datasets.schema import (
    ACTION_ALERT,
    ACTION_PASS,
    CANONICAL_COLUMNS,
    CanonicalBatch,
    PROVENANCE_SYNTHETIC,
    SCHEMA_VERSION,
    SIGNAL_NETWORK_FLOW,
)
from datasets.asset_registry import AssetRegistry


# ---------------------------------------------------------------------------
# Paths — relative to project root
# ---------------------------------------------------------------------------

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_DATASETS_DIR = _PROJECT_ROOT / "datasets"
_CIC_DIR = _DATASETS_DIR / "MachineLearningCVE"
_PAYSIM_FILE = _DATASETS_DIR / "PS_20174392719_1491204439457_log.csv"
_SWAT_DIR = _DATASETS_DIR / "SWaT"


# ---------------------------------------------------------------------------
# Sentinel exception
# ---------------------------------------------------------------------------

class DatasetNotAvailable(Exception):
    """Raised when a requested dataset cannot be found on disk."""


# ---------------------------------------------------------------------------
# Dataset registry
# ---------------------------------------------------------------------------

SUPPORTED_DATASETS = {
    "cic_ids2017": "CIC-IDS2017 network intrusion flows (datasets/MachineLearningCVE/)",
    "paysim":      "PaySim financial transaction simulation (datasets/PS_*.csv)",
    "synthetic":   "AEGIS synthetic generator (no disk files required)",
    "swat":        "SWaT ICS/OT telemetry (datasets/SWaT/merged.csv or attack.csv)",
    "deception":   "Phase 2 honeytoken tripwire events, one per gateway zone (no disk files required)",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_dataset(
    name: str,
    limit: Optional[int] = 50_000,
    registry: Optional[AssetRegistry] = None,
    **adapter_kwargs,
) -> CanonicalBatch:
    """
    Load a named dataset and return a normalised CanonicalBatch.

    Parameters
    ----------
    name :
        One of: "cic_ids2017", "paysim", "synthetic", "swat".
    limit :
        Maximum number of rows to load (None = all rows).
        Default 50 000 keeps interactive loads fast.
    registry :
        Optional shared AssetRegistry.  If None, one is created per call.
    **adapter_kwargs :
        Passed through to the underlying adapter (e.g. fraud_only=True for PaySim).

    Returns
    -------
    CanonicalBatch
        Schema-validated batch ready for ML / CII consumption.

    Raises
    ------
    DatasetNotAvailable
        If the dataset is not on disk or not yet supported.
    ValueError
        If `name` is not a recognised dataset identifier.
    """
    name = name.strip().lower()
    reg = registry or AssetRegistry.from_config()

    if name == "cic_ids2017":
        return _load_cic_ids2017(limit=limit, registry=reg, **adapter_kwargs)
    elif name == "paysim":
        return _load_paysim(limit=limit, registry=reg, **adapter_kwargs)
    elif name == "synthetic":
        return _load_synthetic(limit=limit, registry=reg)
    elif name == "swat":
        return _load_swat(limit=limit, registry=reg, **adapter_kwargs)
    elif name == "deception":
        return _load_deception(limit=limit, registry=reg, **adapter_kwargs)
    else:
        supported = ", ".join(f'"{k}"' for k in SUPPORTED_DATASETS)
        raise ValueError(f"Unknown dataset '{name}'. Supported: {supported}")


def available_datasets() -> dict[str, str]:
    """Return a dict of {name: description} for all registered datasets."""
    available = {}
    for name, desc in SUPPORTED_DATASETS.items():
        if name == "cic_ids2017":
            available[name] = desc if _CIC_DIR.exists() and any(_CIC_DIR.glob("*.csv")) else f"{desc} [NOT FOUND]"
        elif name == "paysim":
            available[name] = desc if _PAYSIM_FILE.exists() else f"{desc} [NOT FOUND]"
        elif name == "swat":
            available[name] = desc if _SWAT_DIR.exists() and any(_SWAT_DIR.glob("*.csv")) else f"{desc} [NOT FOUND]"
        else:
            available[name] = desc
    return available


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_cic_ids2017(
    limit: Optional[int],
    registry: AssetRegistry,
    **kwargs,
) -> CanonicalBatch:
    if not _CIC_DIR.exists() or not any(_CIC_DIR.glob("*.csv")):
        raise DatasetNotAvailable(
            f"CIC-IDS2017 CSVs not found in {_CIC_DIR}. "
            "Download MachineLearningCSV.zip from http://205.174.165.80/CICDataset/"
            "CIC-IDS-2017/Dataset/MachineLearningCSV.zip"
        )
    from datasets.cic_ids_adapter import CICIDSAdapter
    adapter = CICIDSAdapter(data_dir=_CIC_DIR, registry=registry)
    return adapter.load(limit=limit)


def _load_paysim(
    limit: Optional[int],
    registry: AssetRegistry,
    **kwargs,
) -> CanonicalBatch:
    if not _PAYSIM_FILE.exists():
        raise DatasetNotAvailable(
            f"PaySim CSV not found at {_PAYSIM_FILE}. "
            "Download from https://www.kaggle.com/datasets/ealaxi/paysim1"
        )
    from datasets.paysim_adapter import PaySimAdapter
    adapter = PaySimAdapter(
        data_dir=_DATASETS_DIR,
        registry=registry,
        **kwargs,
    )
    return adapter.load(limit=limit)


def _load_swat(
    limit: Optional[int],
    registry: AssetRegistry,
    **kwargs,
) -> CanonicalBatch:
    if not _SWAT_DIR.exists() or not any(_SWAT_DIR.glob("*.csv")):
        raise DatasetNotAvailable(
            f"SWaT CSVs not found in {_SWAT_DIR}. "
            "Place merged.csv or attack.csv in datasets/SWaT/"
        )
    from datasets.swat_adapter import SWaTAdapter
    adapter = SWaTAdapter(data_dir=_SWAT_DIR, registry=registry)
    return adapter.load(limit=limit)


def _load_deception(
    limit: Optional[int],
    registry: AssetRegistry,
    **kwargs,
) -> CanonicalBatch:
    """
    Generate Phase 2 honeytoken tripwire events for every gateway zone
    (Gateway_L0 .. Gateway_L5).

    Standalone source: deliberately NOT merged with synthetic/real traffic
    (unlike the attack-preset recon stage in data_generator.py, which injects
    tripwire rows alongside volumetric attack_edges). This lets a caller
    exercise the tripwire detector / CII gateway-fallback path in isolation.

    `registry` is accepted for interface symmetry with the other `_load_*`
    helpers but unused: tripwire destination ids are already valid dependency-
    graph node names (Gateway_LN), so there is nothing to resolve.

    Extra kwargs
    ------------
    count_per_zone : int, default 1
        Tripwire events generated per gateway zone.
    seed : int, default 42
        Base random seed; each zone gets seed + its index for variety.
    """
    from config import HONEYTOKEN_CREDENTIALS
    from deception.adapter import generate_tripwire_events

    del registry  # unused — see docstring

    count_per_zone = kwargs.pop("count_per_zone", 1)
    seed = kwargs.pop("seed", 42)

    frames = [
        generate_tripwire_events(zone, count=count_per_zone, seed=seed + i)
        for i, zone in enumerate(sorted(HONEYTOKEN_CREDENTIALS))
    ]
    combined = (
        pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=CANONICAL_COLUMNS)
    )
    if limit is not None:
        combined = combined.head(limit)
    return CanonicalBatch.from_dataframe(combined)


def _load_synthetic(
    limit: Optional[int],
    registry: AssetRegistry,
) -> CanonicalBatch:
    """
    Generate a synthetic canonical batch using the existing data_generator.

    Maps generated edges into the canonical schema format so the ML engine
    and CII layer can consume synthetic data through the same interface.
    """
    import sys
    import pathlib as _pl
    _src = _pl.Path(__file__).resolve().parent.parent
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

    from data_generator import generate_mock_network_data
    import pandas as pd
    from datetime import datetime, timezone

    rows = limit or 1_000
    _nodes_df, edges_df = generate_mock_network_data(num_edges=rows)

    # Map synthetic columns → canonical schema
    canonical = pd.DataFrame({
        "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "source_asset_id": edges_df["source"].apply(lambda ip: registry.resolve(ip).asset_name),
        "destination_asset_id": edges_df["target"].apply(lambda ip: registry.resolve(ip).asset_name),
        "protocol": "TCP",
        "payload_size": edges_df.get("bytes", edges_df.get("packets", 1)).astype(float),
        # Ground truth from data_generator's is_ground_truth_anomaly column —
        # was hardcoded to ACTION_PASS for every row (a real bug found while
        # building Phase 3's evaluation harness): the canonical `action`
        # column, which run_evaluation() derives y_true from, was silently
        # always "benign" for the synthetic-fallback dataset regardless of
        # which edges the generator actually labeled anomalous, making any
        # evaluation that fell back to synthetic data degenerate by
        # construction (0% positive rate) instead of reflecting the
        # generator's real ~15% anomaly rate.
        "action": np.where(
            edges_df.get("is_ground_truth_anomaly", False), ACTION_ALERT, ACTION_PASS
        ),
        "zone": "INTERNAL",
        "process_or_service": "SyntheticGenerator",
        "attck_evidence": None,
        "raw_anomaly_score": None,
        "calibrated_alert_level": None,
        "provenance": PROVENANCE_SYNTHETIC,
        "confidence": 0.5,
        "schema_version": SCHEMA_VERSION,
        "signal_type": SIGNAL_NETWORK_FLOW,
        "observed_at": None,
        "purdue_level": None,
        # ML features
        "duration_sec": edges_df.get("duration_sec", 1.0).astype(float),
        "packets": edges_df.get("packets", 1).astype(int),
        "bytes": edges_df.get("bytes", 500).astype(float),
    })
    return CanonicalBatch.from_dataframe(canonical)
