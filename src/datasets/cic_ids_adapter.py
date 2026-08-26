"""
cic_ids_adapter.py — CIC-IDS2017 dataset adapter for AEGIS Phase 1.

Ingests CSVs from datasets/MachineLearningCVE/ and normalises every flow record
into the CanonicalEvent schema.  The adapter deliberately preserves ALL attack
labels (not just binary benign/attack) so the dashboard can display attack families.

Column whitespace: CIC-IDS2017 CSVs have leading/trailing spaces in header names.
This adapter strips them unconditionally before any processing.

Provenance tag: "CIC-IDS2017"
License:       CC BY 4.0 — see DATASETS.md
"""
from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd

from datasets.asset_registry import AssetRegistry
from datasets.schema import (
    ACTION_ALERT,
    ACTION_PASS,
    ALERT_LEVEL_INFO,
    ALERT_LEVEL_CRITICAL,
    ALERT_LEVEL_HIGH,
    ALERT_LEVEL_MEDIUM,
    CANONICAL_COLUMNS,
    PROVENANCE_CIC_IDS2017,
    SCHEMA_VERSION,
    SIGNAL_NETWORK_FLOW,
    CanonicalBatch,
)

# CIC-IDS2017 flows are Purdue Level 3 (site operations / network-layer
# monitoring) — see PLAN_MASTER.md Phase 1, contract C4.
_PURDUE_LEVEL = 3

# ---------------------------------------------------------------------------
# Attack label → (attck_evidence, calibrated_alert_level) mapping
# ---------------------------------------------------------------------------

_LABEL_MAP = {
    "BENIGN":                      (None,                   ALERT_LEVEL_INFO),
    "DDoS":                        ("DDoS",                 ALERT_LEVEL_CRITICAL),
    "DoS slowloris":               ("DoS",                  ALERT_LEVEL_HIGH),
    "DoS Slowhttptest":            ("DoS",                  ALERT_LEVEL_HIGH),
    "DoS Hulk":                    ("DoS",                  ALERT_LEVEL_CRITICAL),
    "DoS GoldenEye":               ("DoS",                  ALERT_LEVEL_HIGH),
    "Heartbleed":                  ("Heartbleed",           ALERT_LEVEL_CRITICAL),
    "PortScan":                    ("Reconnaissance",       ALERT_LEVEL_MEDIUM),
    "Infiltration":                ("Infiltration",         ALERT_LEVEL_CRITICAL),
    "Bot":                         ("Botnet_C2",            ALERT_LEVEL_HIGH),
    "FTP-Patator":                 ("BruteForce_FTP",       ALERT_LEVEL_MEDIUM),
    "SSH-Patator":                 ("BruteForce_SSH",       ALERT_LEVEL_MEDIUM),
    "Web Attack \u2c3e Brute Force":     ("WebAttack_BruteForce", ALERT_LEVEL_HIGH),
    "Web Attack \u2c3e XSS":             ("WebAttack_XSS",        ALERT_LEVEL_MEDIUM),
    "Web Attack \u2c3e Sql Injection":   ("WebAttack_SQLi",       ALERT_LEVEL_CRITICAL),
}

# Catch encoding variants of the Web Attack labels (mojibake from CSV encoding)
_WEB_ATTACK_RE = re.compile(r"Web Attack", re.IGNORECASE)

# Protocol mapping from destination port heuristic
_PORT_PROTOCOL: dict[int, str] = {
    80: "HTTP", 443: "HTTPS", 22: "SSH", 21: "FTP",
    25: "SMTP", 53: "DNS", 3306: "MySQL", 3389: "RDP",
}


def _port_to_protocol(port: int) -> str:
    return _PORT_PROTOCOL.get(int(port), f"PORT_{port}")


def _resolve_label(raw_label: str) -> tuple[Optional[str], str]:
    """Return (attck_evidence, calibrated_alert_level) for a raw CIC label."""
    raw = raw_label.strip()
    if raw in _LABEL_MAP:
        return _LABEL_MAP[raw]
    # Fallback for encoding variants
    if _WEB_ATTACK_RE.search(raw):
        if "SQL" in raw.upper():
            return ("WebAttack_SQLi", ALERT_LEVEL_CRITICAL)
        if "XSS" in raw.upper():
            return ("WebAttack_XSS", ALERT_LEVEL_MEDIUM)
        return ("WebAttack_BruteForce", ALERT_LEVEL_HIGH)
    # Unknown label — treat as alert with medium severity
    return (raw, ALERT_LEVEL_MEDIUM)


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------

class CICIDSAdapter:
    """
    Loads one or more CIC-IDS2017 CSV files and normalises them into CanonicalBatch.

    Parameters
    ----------
    data_dir :
        Path to the directory containing `*_ISCX.csv` files
        (typically `datasets/MachineLearningCVE`).
    registry :
        AssetRegistry for resolving Destination Port to asset names.
        If None, uses AssetRegistry.from_config().
    """

    #: Features kept for the ML engine (numeric columns only, post-normalisation)
    ML_FEATURES = [
        "duration_sec", "packets", "bytes",
    ]

    def __init__(
        self,
        data_dir: pathlib.Path | str,
        registry: Optional[AssetRegistry] = None,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.registry = registry or AssetRegistry.from_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, limit: Optional[int] = 50_000) -> CanonicalBatch:
        """
        Load up to `limit` rows (None = all) from all CSVs in data_dir,
        normalise, and return a single CanonicalBatch.

        Rows are sampled proportionally across files so all attack families
        are represented even at small limits.
        """
        csv_files = sorted(self.data_dir.glob("*.csv"))
        if not csv_files:
            raise FileNotFoundError(
                f"No CSV files found in {self.data_dir}. "
                "Ensure datasets/MachineLearningCVE/ is populated."
            )

        # Deprioritise the all-BENIGN Monday file so attack families appear even at small limits
        attack_files = [f for f in csv_files if "Monday" not in f.name]
        benign_only = [f for f in csv_files if "Monday" in f.name]
        ordered_files = attack_files + benign_only

        per_file_limit = (limit // max(len(attack_files), 1)) if limit else None

        frames: List[pd.DataFrame] = []
        for csv_path in ordered_files:
            chunk = self._load_file(csv_path, per_file_limit)
            if chunk is not None and not chunk.empty:
                frames.append(chunk)

        if not frames:
            return CanonicalBatch(pd.DataFrame(columns=CANONICAL_COLUMNS))

        combined = pd.concat(frames, ignore_index=True)
        return CanonicalBatch.from_dataframe(combined)

    def load_file(
        self, filename: str, limit: Optional[int] = 50_000
    ) -> CanonicalBatch:
        """Load a single named CSV file from data_dir."""
        path = self.data_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"{path} not found.")
        df = self._load_file(path, limit)
        if df is None or df.empty:
            return CanonicalBatch(pd.DataFrame(columns=CANONICAL_COLUMNS))
        return CanonicalBatch.from_dataframe(df)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_file(
        self, path: pathlib.Path, limit: Optional[int]
    ) -> Optional[pd.DataFrame]:
        try:
            raw = pd.read_csv(path, low_memory=False)
        except Exception as exc:
            print(f"[CICIDSAdapter] Warning: could not read {path}: {exc}")
            return None

        # Strip whitespace from all column names
        raw.columns = raw.columns.str.strip()

        # Drop rows with any NaN in required columns
        required = ["Destination Port", "Total Fwd Packets", "Total Length of Fwd Packets",
                    "Flow Duration", "Label"]
        raw = raw.dropna(subset=required)

        if raw.empty:
            return None

        # Stratified sample: ensure attack rows appear even at small limits
        if limit and len(raw) > limit:
            benign = raw[raw["Label"].str.strip() == "BENIGN"]
            attacks = raw[raw["Label"].str.strip() != "BENIGN"]
            if len(attacks) > 0:
                # Take up to limit//2 attacks, rest from benign
                n_attacks = min(len(attacks), limit // 2)
                n_benign = min(len(benign), limit - n_attacks)
                raw = pd.concat([
                    attacks.sample(n=n_attacks, random_state=42),
                    benign.sample(n=n_benign, random_state=42),
                ], ignore_index=True)
            else:
                raw = raw.sample(n=min(len(raw), limit), random_state=42)

        return self._normalise(raw)

    def _normalise(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Convert raw CIC-IDS2017 columns into canonical schema columns."""
        # --- Label resolution ---
        labels = raw["Label"].str.strip()
        attck_evidence = []
        calibrated_alert_level = []
        actions = []
        for lbl in labels:
            ev, lvl = _resolve_label(lbl)
            attck_evidence.append(ev)
            calibrated_alert_level.append(lvl)
            actions.append(ACTION_PASS if ev is None else ACTION_ALERT)

        # --- Asset resolution ---
        # CIC-IDS2017 doesn't include source/destination IPs — flows are unlabelled at
        # the IP level. We assign well-known ports to infrastructure nodes and treat the
        # attacking side as the external threat pool.
        dest_ports = pd.to_numeric(raw["Destination Port"], errors="coerce").fillna(0).astype(int)

        source_ids = []
        dest_ids = []
        confidences = []

        for i, (port, action) in enumerate(zip(dest_ports, actions)):
            # If it's a threat, source is attacker (external), dest is internal asset
            if action == ACTION_ALERT:
                # Attacker → internal asset serving that port
                src_result = self.registry.resolve("185.220.101.5")  # representative external IP
                dst_result = self._port_to_asset(port)
            else:
                # Normal internal traffic — approximate by port
                src_result = self._port_to_asset(port)
                dst_result = self.registry.resolve("10.0.1.15")  # Citizen_Portal
            source_ids.append(src_result.asset_name)
            dest_ids.append(dst_result.asset_name)
            confidences.append((src_result.confidence + dst_result.confidence) / 2.0)

        # --- Feature mapping ---
        duration_us = pd.to_numeric(raw["Flow Duration"], errors="coerce").fillna(0)
        bytes_fwd = pd.to_numeric(raw["Total Length of Fwd Packets"], errors="coerce").fillna(0)
        pkts_fwd = pd.to_numeric(raw["Total Fwd Packets"], errors="coerce").fillna(1)
        protocols = dest_ports.apply(_port_to_protocol)

        return pd.DataFrame({
            "timestamp": datetime(2017, 7, 3, 9, 0, 0, tzinfo=timezone.utc),  # dataset week
            "source_asset_id": source_ids,
            "destination_asset_id": dest_ids,
            "protocol": protocols.values,
            "payload_size": bytes_fwd.values,
            "action": actions,
            "zone": ["INTERNAL" if a == ACTION_PASS else "DMZ" for a in actions],
            "process_or_service": protocols.values,
            "attck_evidence": attck_evidence,
            "raw_anomaly_score": None,
            "calibrated_alert_level": calibrated_alert_level,
            "provenance": PROVENANCE_CIC_IDS2017,
            "confidence": confidences,
            "schema_version": SCHEMA_VERSION,
            "signal_type": SIGNAL_NETWORK_FLOW,
            "observed_at": None,
            "purdue_level": _PURDUE_LEVEL,
            # Extra numeric features kept for ML engine
            "duration_sec": (duration_us / 1_000_000).clip(lower=0).values,
            "packets": pkts_fwd.astype(int).values,
            "bytes": bytes_fwd.values,
        })

    def _port_to_asset(self, port: int):
        """Heuristically map a destination port to a smart-city asset."""
        port_to_ip = {
            80: "10.0.1.15",   # Citizen_Portal
            443: "10.0.1.15",  # Citizen_Portal
            22: "10.0.1.16",   # SCADA_Historian (SSH admin)
            21: "10.0.1.16",   # SCADA_Historian (FTP)
            3306: "10.0.1.16", # SCADA_Historian (DB)
            3389: "10.0.1.12", # Traffic_Controller (RDP)
            53: "10.0.1.14",   # Emergency_Route_System (DNS)
        }
        ip = port_to_ip.get(port, "10.0.1.15")  # default Citizen_Portal
        return self.registry.resolve(ip)
