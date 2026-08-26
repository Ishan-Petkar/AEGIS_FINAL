import pathlib
from typing import Optional

import pandas as pd
import numpy as np

from datasets.schema import (
    CanonicalBatch,
    ACTION_ALERT,
    ACTION_PASS,
    PROVENANCE_SWAT,
    SCHEMA_VERSION,
    SIGNAL_ICS_READING,
)
from datasets.asset_registry import AssetRegistry

# SWaT sensor/actuator telemetry is Purdue Level 1 (basic control — PLCs and
# field instrumentation) — see PLAN_MASTER.md Phase 1, contract C4.
_PURDUE_LEVEL = 1


class SWaTAdapter:
    """Adapter for the SWaT (Secure Water Treatment) dataset."""

    def __init__(self, data_dir: pathlib.Path, registry: AssetRegistry):
        self.data_dir = data_dir
        self.registry = registry
        # Prioritize merged.csv, fallback to attack.csv if merged isn't present
        self.file_path = self.data_dir / "merged.csv"
        if not self.file_path.exists():
            self.file_path = self.data_dir / "attack.csv"

    def load(self, limit: Optional[int] = None) -> CanonicalBatch:
        if not self.file_path.exists():
            raise FileNotFoundError(f"SWaT dataset file not found: {self.file_path}")

        # Optimization: pandas can be slow reading many rows. Use engine='c' which is default.
        df = pd.read_csv(self.file_path, nrows=limit)

        # Clean column names (SWaT has leading spaces e.g. " Timestamp", " MV101")
        df.columns = df.columns.str.strip()

        canonical = pd.DataFrame()
        # Parse timestamp: e.g. "28/12/2015 10:00:00 AM"
        canonical["timestamp"] = pd.to_datetime(
            df["Timestamp"],
            format="%d/%m/%Y %I:%M:%S %p",
            errors='coerce',
            utc=True
        )
        # Fallback for alternative formats if the above format fails
        if canonical["timestamp"].isna().any():
            canonical["timestamp"] = pd.to_datetime(
                df["Timestamp"],
                format="mixed",
                errors='coerce',
                utc=True
            )

        canonical["source_asset_id"] = "SWaT_System"
        canonical["destination_asset_id"] = "SWaT_System"
        canonical["protocol"] = "OT_Telemetry"
        canonical["payload_size"] = 1.0

        # Map Normal/Attack to canonical action
        is_attack = df["Normal/Attack"].str.strip().str.lower() == "attack"
        canonical["action"] = np.where(is_attack, ACTION_ALERT, ACTION_PASS)

        canonical["zone"] = "ICS"
        canonical["process_or_service"] = "SensorTelemetry"
        canonical["attck_evidence"] = None
        canonical["raw_anomaly_score"] = None
        canonical["calibrated_alert_level"] = None
        canonical["provenance"] = PROVENANCE_SWAT
        canonical["confidence"] = 1.0
        canonical["schema_version"] = SCHEMA_VERSION
        canonical["signal_type"] = SIGNAL_ICS_READING
        canonical["observed_at"] = pd.NaT
        canonical["purdue_level"] = _PURDUE_LEVEL

        # Merge all original sensor columns into the canonical dataframe
        # These are used as features by the ML engine
        sensor_cols = [c for c in df.columns if c not in ["Timestamp", "Normal/Attack"]]
        for col in sensor_cols:
            canonical[col] = df[col]

        return CanonicalBatch.from_dataframe(canonical)
