"""
adapter.py — Honeytoken tripwire event emission for AEGIS Phase 2.

Generates synthetic "deception_tripwire" signal events: an attacker probing a
Purdue-zone gateway with that zone's honeytoken credential
(config.HONEYTOKEN_CREDENTIALS). Every honeytoken has zero legitimate use, so
any touch is unambiguous compromise — no threshold, no learned pattern
(detectors/tripwire.py just reads the boolean this module sets).

Per contract C4 (datasets/schema.py SCHEMA_VERSION 2.0), a tripwire event must
never fabricate flow-shaped fields: duration_sec, packets, and bytes are
always 0 here, never derived from payload_size. Fabricating them would make a
honeytoken touch look like a volume anomaly, reintroducing the circular
labeling C4 exists to prevent.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from config import HONEYTOKEN_CREDENTIALS
from datasets.schema import (
    ACTION_ALERT,
    ALERT_LEVEL_CRITICAL,
    CANONICAL_COLUMNS,
    SCHEMA_VERSION,
    SIGNAL_DECEPTION_TRIPWIRE,
)

#: Source id for the (unknown, by construction) actor probing the honeytoken.
#: A honeytoken credential has no legitimate owner, so any user of it is, by
#: definition, an unidentified external probe.
UNKNOWN_PROBE_SOURCE = "Unknown_External_Probe"

#: Modeled detection latency for a tripwire hit — this is a deterministic
#: credential-match, not investigation time; it represents the (near-instant)
#: time to log and surface the alert.
_DETECTION_LATENCY_SEC = 1.0

#: Non-canonical ML feature columns every adapter attaches (Phase 1 pattern);
#: always zero here per contract C4, plus the boolean the tripwire detector
#: reads.
_EXTRA_COLUMNS = ["duration_sec", "packets", "bytes", "is_honeytoken_use"]


def _purdue_level_of(gateway_zone: str) -> Optional[int]:
    """Extract the Purdue level from a 'Gateway_LN' name, e.g. 'Gateway_L4' -> 4."""
    try:
        return int(gateway_zone.rsplit("L", 1)[-1])
    except (ValueError, IndexError):
        return None


def generate_tripwire_events(
    gateway_zone: str,
    count: int = 1,
    seed: int = 42,
    base_timestamp: Optional[datetime] = None,
) -> pd.DataFrame:
    """
    Generate `count` synthetic honeytoken-use events for `gateway_zone`.

    Parameters
    ----------
    gateway_zone:
        A key of config.HONEYTOKEN_CREDENTIALS (e.g. "Gateway_L4"). This is
        also the graph_manager.py gateway node name that guards that Purdue
        zone, so `destination_asset_id` doubles as a valid dependency-graph
        node name for downstream CII tracing.
    count:
        Number of tripwire events to generate.
    seed:
        Random seed (reproducibility; also perturbs inter-event spacing).
    base_timestamp:
        Event generation time. Defaults to now (UTC).

    Returns
    -------
    pd.DataFrame
        Canonical schema columns (CANONICAL_COLUMNS) plus the ML-feature
        columns duration_sec/packets/bytes (always 0 — never fabricated, per
        contract C4) and `is_honeytoken_use` (always True).

    Raises
    ------
    KeyError
        If `gateway_zone` is not in config.HONEYTOKEN_CREDENTIALS.
    """
    if gateway_zone not in HONEYTOKEN_CREDENTIALS:
        raise KeyError(
            f"Unknown gateway zone {gateway_zone!r}. "
            f"Expected one of {sorted(HONEYTOKEN_CREDENTIALS)}."
        )
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")

    credential = HONEYTOKEN_CREDENTIALS[gateway_zone]
    rng = random.Random(seed)
    base_timestamp = base_timestamp or datetime.now(timezone.utc)
    purdue_level = _purdue_level_of(gateway_zone)

    columns = CANONICAL_COLUMNS + _EXTRA_COLUMNS
    if count == 0:
        return pd.DataFrame(columns=columns)

    rows = []
    for i in range(count):
        ts = base_timestamp + timedelta(seconds=rng.uniform(0, 1) + i)
        observed_at = ts + timedelta(seconds=_DETECTION_LATENCY_SEC)
        rows.append({
            "timestamp": ts,
            "source_asset_id": UNKNOWN_PROBE_SOURCE,
            "destination_asset_id": gateway_zone,
            "protocol": credential["emulated_protocol"],
            "payload_size": 0.0,
            "action": ACTION_ALERT,
            "zone": gateway_zone,
            "process_or_service": "HoneytokenTripwire",
            "attck_evidence": f"T1078_Valid_Accounts::{credential['credential_id']}",
            "raw_anomaly_score": 1.0,
            "calibrated_alert_level": ALERT_LEVEL_CRITICAL,
            "provenance": "Deception",
            "confidence": 1.0,
            "schema_version": SCHEMA_VERSION,
            "signal_type": SIGNAL_DECEPTION_TRIPWIRE,
            "observed_at": observed_at,
            "purdue_level": purdue_level,
            # ML feature columns — NEVER fabricated for a tripwire (contract
            # C4): a honeytoken touch has no real flow volume.
            "duration_sec": 0.0,
            "packets": 0,
            "bytes": 0.0,
            "is_honeytoken_use": True,
        })

    return pd.DataFrame(rows, columns=columns)
