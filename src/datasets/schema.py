"""
schema.py — Canonical Event Schema for AEGIS Phase 1.

Every ingestion path (CIC-IDS2017, PaySim, SWaT, synthetic generator, live stream)
MUST normalise events into CanonicalEvent before any downstream component (ML engine,
CII calculator, dashboard) processes them.  No component is permitted to consume a
dataset-specific DataFrame column directly.

Schema Version: 2.0 — adds signal_type, observed_at, and purdue_level (contract C4,
PLAN_MASTER.md Phase 1). These exist so a non-flow signal (e.g. a Phase 2 deception
tripwire) can be represented without fabricating flow-shaped fields (duration_sec/
packets/bytes) it doesn't actually have.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "2.0"

# Provenance tags — every event carries one of these
PROVENANCE_CIC_IDS2017 = "CIC-IDS2017"
PROVENANCE_PAYSIM = "PaySim"
PROVENANCE_SWAT = "SWaT"
PROVENANCE_SYNTHETIC = "synthetic"
PROVENANCE_LIVE = "live"

# Signal types — what kind of thing this event represents, not just where it
# came from. Determines which feature-extraction path to_ml_features() uses.
SIGNAL_NETWORK_FLOW = "network_flow"
SIGNAL_FINANCIAL_TXN = "financial_txn"
SIGNAL_ICS_READING = "ics_reading"
SIGNAL_DECEPTION_TRIPWIRE = "deception_tripwire"
VALID_SIGNAL_TYPES = {
    SIGNAL_NETWORK_FLOW,
    SIGNAL_FINANCIAL_TXN,
    SIGNAL_ICS_READING,
    SIGNAL_DECEPTION_TRIPWIRE,
}

# Normalised action verbs
ACTION_PASS = "PASS"
ACTION_ALERT = "ALERT"
ACTION_DENY = "DENY"
ACTION_PAYMENT = "PAYMENT"
ACTION_CASH_OUT = "CASH_OUT"
ACTION_CASH_IN = "CASH_IN"
ACTION_DEBIT = "DEBIT"
ACTION_TRANSFER = "TRANSFER"

# Alert levels
ALERT_LEVEL_CRITICAL = "CRITICAL"
ALERT_LEVEL_HIGH = "HIGH"
ALERT_LEVEL_MEDIUM = "MEDIUM"
ALERT_LEVEL_LOW = "LOW"
ALERT_LEVEL_INFO = "INFO"

# Columns in the canonical DataFrame representation (same order, always)
CANONICAL_COLUMNS = [
    "timestamp",
    "source_asset_id",
    "destination_asset_id",
    "protocol",
    "payload_size",
    "action",
    "zone",
    "process_or_service",
    "attck_evidence",
    "raw_anomaly_score",
    "calibrated_alert_level",
    "provenance",
    "confidence",
    "schema_version",
    "signal_type",
    "observed_at",
    "purdue_level",
]


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------

class CanonicalEvent(BaseModel):
    """
    A single normalised security/telemetry event.

    All adapters produce instances of this model.  The ML engine and CII
    calculator consume it — never raw dataset formats.
    """

    model_config = {"frozen": False}

    timestamp: datetime = Field(
        description="UTC event timestamp."
    )
    source_asset_id: str = Field(
        min_length=1,
        description="Resolved asset name or raw identifier (IP, account ID).",
    )
    destination_asset_id: str = Field(
        min_length=1,
        description="Resolved asset name or raw identifier.",
    )
    protocol: str = Field(
        description="Network protocol or transaction type (e.g. TCP, FINANCIAL_TRANSFER).",
    )
    payload_size: float = Field(
        ge=0.0,
        description="Bytes transferred (network) or transaction amount (financial).",
    )
    action: str = Field(
        description="Normalised action verb: PASS, ALERT, DENY, PAYMENT, CASH_OUT, …",
    )
    zone: str = Field(
        description="Network / operational zone (INTERNAL, DMZ, FINANCIAL_EXTERNAL, ICS).",
    )
    process_or_service: str = Field(
        description="Service, protocol, or business process responsible for the event.",
    )
    attck_evidence: Optional[str] = Field(
        default=None,
        description="MITRE ATT&CK technique, attack family, or fraud type. None if benign.",
    )
    raw_anomaly_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Normalised [0,1] ML anomaly score assigned after detection. Null at ingestion.",
    )
    calibrated_alert_level: Optional[str] = Field(
        default=None,
        description="Conformal-calibrated alert severity (CRITICAL/HIGH/MEDIUM/LOW/INFO).",
    )
    provenance: str = Field(
        description="Dataset or source identifier. One of the PROVENANCE_* constants.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Asset resolution confidence. 1.0 = certain mapping, lower = inferred.",
    )
    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema version string for forward-compatibility tracking.",
    )
    signal_type: str = Field(
        default=SIGNAL_NETWORK_FLOW,
        description=(
            "Category of signal this event represents: network_flow, financial_txn, "
            "ics_reading, or deception_tripwire. A deception_tripwire has no real "
            "flow volume — to_ml_features() must not fabricate duration_sec/packets/"
            "bytes for it from payload_size."
        ),
    )
    observed_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Wall-clock time AEGIS detected/observed this event, distinct from "
            "`timestamp` (when the underlying event occurred in the source system). "
            "None means no detection latency is modeled — treat as == timestamp."
        ),
    )
    purdue_level: Optional[int] = Field(
        default=None,
        ge=0,
        le=5,
        description=(
            "ISA-95/Purdue Enterprise Reference Architecture level of the asset that "
            "originated this event (0=physical process/sensors ... 5=enterprise/"
            "external). None if not yet classified."
        ),
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, v):
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        return v

    @field_validator("signal_type")
    @classmethod
    def _validate_signal_type(cls, v):
        if v not in VALID_SIGNAL_TYPES:
            raise ValueError(
                f"signal_type must be one of {sorted(VALID_SIGNAL_TYPES)}, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _strip_whitespace(self):
        for field in ("source_asset_id", "destination_asset_id", "protocol", "action", "zone", "process_or_service", "provenance"):
            val = getattr(self, field)
            if isinstance(val, str):
                object.__setattr__(self, field, val.strip())
        return self

    def to_dict(self) -> dict:
        """Return ordered dict suitable for pandas row construction."""
        return {
            "timestamp": self.timestamp,
            "source_asset_id": self.source_asset_id,
            "destination_asset_id": self.destination_asset_id,
            "protocol": self.protocol,
            "payload_size": self.payload_size,
            "action": self.action,
            "zone": self.zone,
            "process_or_service": self.process_or_service,
            "attck_evidence": self.attck_evidence,
            "raw_anomaly_score": self.raw_anomaly_score,
            "calibrated_alert_level": self.calibrated_alert_level,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "schema_version": self.schema_version,
            "signal_type": self.signal_type,
            "observed_at": self.observed_at,
            "purdue_level": self.purdue_level,
        }


# ---------------------------------------------------------------------------
# CanonicalBatch — high-performance bulk operations
# ---------------------------------------------------------------------------

class CanonicalBatch:
    """
    Wraps a pandas DataFrame that conforms to the canonical event schema.

    Provides:
    - validate_schema()      — verify all required columns are present
    - to_ml_features()       — extract numeric feature matrix for ml_engine
    - is_alert()             — boolean series of rows with attck_evidence
    - filter_by_provenance() — slice by dataset name
    """

    REQUIRED_COLUMNS = set(CANONICAL_COLUMNS)

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    @classmethod
    def from_events(cls, events: List[CanonicalEvent]) -> "CanonicalBatch":
        """Build a CanonicalBatch from a list of CanonicalEvent objects."""
        if not events:
            return cls(pd.DataFrame(columns=CANONICAL_COLUMNS))
        rows = [e.to_dict() for e in events]
        return cls(pd.DataFrame(rows, columns=CANONICAL_COLUMNS))

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame) -> "CanonicalBatch":
        """Wrap an already-built canonical DataFrame."""
        batch = cls(df)
        batch.validate_schema()
        return batch

    def validate_schema(self) -> None:
        """Raise ValueError if any required column is missing."""
        missing = self.REQUIRED_COLUMNS - set(self._df.columns)
        if missing:
            raise ValueError(
                f"CanonicalBatch is missing required columns: {sorted(missing)}"
            )

    @property
    def df(self) -> pd.DataFrame:
        """Return the underlying DataFrame (read-only by convention)."""
        return self._df

    def is_alert(self) -> pd.Series:
        """Boolean Series: True where attck_evidence is not null (i.e., a flagged event)."""
        return self._df["attck_evidence"].notna()

    def filter_by_provenance(self, provenance: str) -> "CanonicalBatch":
        """Return a new batch filtered to a specific provenance tag."""
        return CanonicalBatch(
            self._df[self._df["provenance"] == provenance].reset_index(drop=True)
        )

    def to_ml_features(self) -> pd.DataFrame:
        """
        Extract numeric features for the ML engine from the canonical batch.

        Maps canonical fields to the feature names expected by ml_engine.preprocess_features.
        Additional dataset-specific features can be added as extra columns before calling
        this; they will be ignored by ml_engine if not in its feature list.

        Dispatches on `signal_type`: a deception_tripwire event (Phase 2) has no real
        network-flow volume, so its duration_sec/packets/bytes are zeroed rather than
        fabricated from payload_size — fabricating them would make a tripwire touch
        look like a volume anomaly, reintroducing circular labeling (see PLAN_MASTER.md
        Phase 1, contract C4).
        """
        packets_col = self._df["packets"] if "packets" in self._df.columns else pd.Series(1, index=self._df.index)
        duration_col = self._df["duration_sec"] if "duration_sec" in self._df.columns else (self._df["payload_size"] / 1e6).clip(lower=0)
        bytes_col = self._df["payload_size"].clip(lower=0).astype(float)

        duration_col = duration_col.astype(float)
        packets_col = pd.to_numeric(packets_col, errors="coerce").fillna(1).astype(int)

        if "signal_type" in self._df.columns:
            is_tripwire = self._df["signal_type"] == SIGNAL_DECEPTION_TRIPWIRE
            duration_col = duration_col.where(~is_tripwire, 0.0)
            packets_col = packets_col.where(~is_tripwire, 0).astype(int)
            bytes_col = bytes_col.where(~is_tripwire, 0.0)

        features = pd.DataFrame({
            "duration_sec": duration_col,
            "packets": packets_col,
            "bytes": bytes_col,
            "source_asset_id": self._df["source_asset_id"],
            "destination_asset_id": self._df["destination_asset_id"],
        }, index=self._df.index)
        return features

    def __len__(self) -> int:
        return len(self._df)

    def __repr__(self) -> str:
        alerts = int(self.is_alert().sum())
        return (
            f"CanonicalBatch(rows={len(self._df)}, alerts={alerts}, "
            f"provenances={self._df['provenance'].unique().tolist()})"
        )
