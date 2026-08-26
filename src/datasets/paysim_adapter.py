"""
paysim_adapter.py — PaySim dataset adapter for AEGIS Phase 1.

Ingests PS_20174392719_1491204439457_log.csv (~6.4M rows) and normalises every
transaction into the CanonicalEvent schema.

PaySim is synthetic but widely used and explicitly endorsed by the roadmap for the
financial anomaly angle.  The adapter surfaces fraud flags (isFraud=1) as
attck_evidence="TRANSFER_FRAUD".

Provenance tag: "PaySim"
License:       CC BY-SA 4.0 — see DATASETS.md
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from datasets.asset_registry import AssetRegistry
from datasets.schema import (
    ACTION_ALERT,
    ACTION_CASH_IN,
    ACTION_CASH_OUT,
    ACTION_DEBIT,
    ACTION_PASS,
    ACTION_PAYMENT,
    ACTION_TRANSFER,
    ALERT_LEVEL_CRITICAL,
    ALERT_LEVEL_INFO,
    CANONICAL_COLUMNS,
    PROVENANCE_PAYSIM,
    SCHEMA_VERSION,
    SIGNAL_FINANCIAL_TXN,
    CanonicalBatch,
)

# PaySim transactions are Purdue Level 4 (business logistics / enterprise
# financial systems) — see PLAN_MASTER.md Phase 1, contract C4.
_PURDUE_LEVEL = 4

# ---------------------------------------------------------------------------
# Transaction type mappings
# ---------------------------------------------------------------------------

# PaySim `type` → (action, process_or_service, protocol)
_TYPE_MAP = {
    "PAYMENT":   (ACTION_PAYMENT,  "PaymentService",   "FINANCIAL_PAYMENT"),
    "TRANSFER":  (ACTION_TRANSFER, "TransferService",  "FINANCIAL_TRANSFER"),
    "CASH_OUT":  (ACTION_CASH_OUT, "CashOutService",   "FINANCIAL_CASH_OUT"),
    "DEBIT":     (ACTION_DEBIT,    "DebitService",     "FINANCIAL_DEBIT"),
    "CASH_IN":   (ACTION_CASH_IN,  "CashInService",    "FINANCIAL_CASH_IN"),
}

# Transaction types where fraud most commonly occurs in PaySim
_HIGH_RISK_TYPES = {"TRANSFER", "CASH_OUT"}

# step in PaySim = 1 hour from simulation start (Jan 1, 2021 00:00 UTC)
_EPOCH = datetime(2021, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _step_to_timestamp(step: int) -> datetime:
    from datetime import timedelta
    return _EPOCH + timedelta(hours=int(step))


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------

class PaySimAdapter:
    """
    Loads the PaySim CSV and normalises transactions into CanonicalBatch.

    Parameters
    ----------
    data_dir :
        Directory containing the PaySim CSV file.
    filename :
        Name of the PaySim CSV (default: PS_20174392719_1491204439457_log.csv).
    registry :
        AssetRegistry for resolving account IDs to graph nodes.
    fraud_only :
        If True, return only fraudulent transactions (isFraud == 1).
        Useful for lightweight dashboard loading.
    """

    FILENAME = "PS_20174392719_1491204439457_log.csv"

    def __init__(
        self,
        data_dir: pathlib.Path | str,
        filename: str = FILENAME,
        registry: Optional[AssetRegistry] = None,
        fraud_only: bool = False,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.filename = filename
        self.registry = registry or AssetRegistry.from_config()
        self.fraud_only = fraud_only

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, limit: Optional[int] = 50_000) -> CanonicalBatch:
        """
        Load up to `limit` rows from the PaySim CSV and normalise.

        When fraud_only=False (default), the loader samples proportionally
        to keep the real fraud rate (~0.1%) visible — otherwise small limits
        would contain zero fraud events.
        """
        path = self.data_dir / self.filename
        if not path.exists():
            raise FileNotFoundError(
                f"PaySim CSV not found at {path}. "
                f"Download from https://www.kaggle.com/datasets/ealaxi/paysim1"
            )

        if self.fraud_only:
            # Stream through and collect fraud rows only (no full load needed)
            chunks = pd.read_csv(path, chunksize=100_000)
            fraud_frames = []
            collected = 0
            for chunk in chunks:
                fraudulent = chunk[chunk["isFraud"] == 1]
                fraud_frames.append(fraudulent)
                collected += len(fraudulent)
                if limit and collected >= limit:
                    break
            if not fraud_frames:
                return CanonicalBatch(pd.DataFrame(columns=CANONICAL_COLUMNS))
            raw = pd.concat(fraud_frames, ignore_index=True)
            if limit:
                raw = raw.head(limit)
        else:
            # Mixed load: guarantee at least some fraud rows by stacking a fraud sample
            # on top of a regular sample.
            fraud_sample_size = max(500, (limit or 50_000) // 20) if limit else None
            benign_size = (limit - fraud_sample_size) if limit and fraud_sample_size else None

            # Load full index from disk to enable sampling
            raw = pd.read_csv(path, nrows=limit)
            # top-up with fraud rows if they're sparse
            fraud_in_sample = raw[raw["isFraud"] == 1]
            if len(fraud_in_sample) < 50 and fraud_sample_size:
                fraud_extra = pd.read_csv(
                    path,
                    skiprows=lambda i: i > 0 and i < 500_000,  # skip first 500k to find fraud
                    nrows=500_000,
                )
                fraud_extra = fraud_extra[fraud_extra["isFraud"] == 1].head(fraud_sample_size)
                raw = pd.concat([raw.head(benign_size or limit), fraud_extra], ignore_index=True)

        return CanonicalBatch.from_dataframe(self._normalise(raw))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalise(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Convert raw PaySim columns to canonical schema columns."""

        # --- Timestamps ---
        timestamps = raw["step"].apply(_step_to_timestamp)

        # --- Transaction type → action / protocol ---
        actions = []
        processes = []
        protocols = []
        for t in raw["type"]:
            a, p, pr = _TYPE_MAP.get(str(t).upper(), (ACTION_PASS, "UnknownService", "FINANCIAL_UNKNOWN"))
            actions.append(a)
            processes.append(p)
            protocols.append(pr)

        # --- Fraud flag → attck_evidence + alert level ---
        is_fraud = raw["isFraud"].astype(int)
        attck_evidence = is_fraud.map({1: "TRANSFER_FRAUD", 0: None})
        calibrated_alert_level = is_fraud.map({1: ALERT_LEVEL_CRITICAL, 0: ALERT_LEVEL_INFO})
        final_actions = [
            ACTION_ALERT if fraud else action
            for fraud, action in zip(is_fraud, actions)
        ]

        # --- Asset resolution ---
        source_ids = []
        dest_ids = []
        confidences = []
        for orig, dest in zip(raw["nameOrig"], raw["nameDest"]):
            src_res = self.registry.resolve(str(orig))
            dst_res = self.registry.resolve(str(dest))
            source_ids.append(src_res.asset_name)
            dest_ids.append(dst_res.asset_name)
            confidences.append((src_res.confidence + dst_res.confidence) / 2.0)

        # --- Zone ---
        zones = raw["type"].apply(
            lambda t: "FINANCIAL_EXTERNAL"
            if str(t).upper() in _HIGH_RISK_TYPES
            else "FINANCIAL_INTERNAL"
        )

        return pd.DataFrame({
            "timestamp": timestamps.values,
            "source_asset_id": source_ids,
            "destination_asset_id": dest_ids,
            "protocol": protocols,
            "payload_size": raw["amount"].clip(lower=0).values,
            "action": final_actions,
            "zone": zones.values,
            "process_or_service": processes,
            "attck_evidence": attck_evidence.values,
            "raw_anomaly_score": None,
            "calibrated_alert_level": calibrated_alert_level.values,
            "provenance": PROVENANCE_PAYSIM,
            "confidence": confidences,
            "schema_version": SCHEMA_VERSION,
            "signal_type": SIGNAL_FINANCIAL_TXN,
            "observed_at": None,
            "purdue_level": _PURDUE_LEVEL,
            # Extra columns for ML engine feature engineering
            "duration_sec": 0.0,   # PaySim has no duration
            "packets": 1,
            "bytes": raw["amount"].clip(lower=0).values,
        })
