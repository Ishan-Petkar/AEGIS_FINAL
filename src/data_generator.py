"""
data_generator.py — Synthetic network traffic generator for AEGIS.

Produces node and edge DataFrames that feed into the ML anomaly detection engine.
All numeric parameters are drawn from SETTINGS (settings.py).
"""

import datetime
import random

import numpy as np
import pandas as pd

from config import EXTERNAL_THREAT_IPS, SMART_CITY_ASSETS
from settings import SETTINGS

# Phase 2: which gateway zone's honeytoken each scripted attack preset
# (aegis_demo.py sidebar buttons) uses for its recon stage. Deliberately
# mixes zones that DO guard a protected asset in DEPENDENCY_GRAPH (Gateway_L4
# -> City_Payment_Gateway/Social_Welfare_System, Gateway_L3 ->
# Emergency_Route_System) with zones that currently guard none
# (Gateway_L0, Gateway_L2 — no purdue_level 0 or 2 asset meets the gateway
# criticality threshold today). Both cases must produce a non-zero CII; see
# cii_calculator.compute_cascading_impact_full's tripwire-only branch.
ATTACK_RECON_GATEWAY = {
    "Payment Gateway Breach": "Gateway_L4",
    "Camera Spoofing": "Gateway_L0",
    "Data Exfiltration": "Gateway_L2",
    "Lateral Movement": "Gateway_L3",
}


def generate_mock_network_data(
    num_nodes: int | None = None,
    num_edges: int | None = None,
    anomaly_rate: float | None = None,
    seed: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate synthetic network traffic data.

    Parameters
    ----------
    num_nodes:
        Number of synthetic nodes. Defaults to SETTINGS.data_gen.num_nodes.
    num_edges:
        Number of synthetic edge events. Defaults to SETTINGS.data_gen.num_edges.
    anomaly_rate:
        Probability of injecting a large-volume anomaly when the source is a
        known threat IP. Defaults to SETTINGS.data_gen.anomaly_rate.
    seed:
        Random seed for reproducibility. Defaults to SETTINGS.data_gen.random_seed.

    Returns
    -------
    nodes_df:
        DataFrame of assets (smart city, financial, external threat nodes).
    edges_df:
        DataFrame of traffic connections with timestamps and metrics.
    """
    cfg = SETTINGS.data_gen
    num_nodes = num_nodes if num_nodes is not None else cfg.num_nodes
    num_edges = num_edges if num_edges is not None else cfg.num_edges
    anomaly_rate = anomaly_rate if anomaly_rate is not None else cfg.anomaly_rate
    seed = seed if seed is not None else cfg.random_seed

    np.random.seed(seed)
    random.seed(seed)

    # --- Build nodes DataFrame ---
    assets: list[dict] = SMART_CITY_ASSETS.copy()
    assets.extend(EXTERNAL_THREAT_IPS)
    nodes_df = pd.DataFrame(assets)

    valid_ips: list[str] = [a["ip"] for a in assets if a["ip"]]

    edges: list[dict] = []

    # Baseline normal connections (fixed topology flows)
    baseline: list[tuple[str, str]] = [
        ("10.0.1.10", "10.0.1.12"),  # Cam 1  → Controller
        ("10.0.1.11", "10.0.1.12"),  # Cam 2  → Controller
        ("10.0.1.12", "10.0.1.13"),  # Controller → Substation
        ("10.0.1.16", "10.0.1.12"),  # Historian → Controller
        ("10.0.1.15", "10.0.1.20"),  # Portal → Payment Gateway
        ("10.0.1.20", "10.0.1.21"),  # Gateway → Bank API
    ]
    for _ in range(cfg.baseline_repeat_count):
        for src, dst in baseline:
            edges.append(
                {
                    "source": src,
                    "target": dst,
                    "duration_sec": round(float(np.random.uniform(0.1, 2.0)), 3),
                    "packets": int(np.random.uniform(5, 20)),
                    "bytes": int(np.random.uniform(500, 5000)),
                    "timestamp": (
                        datetime.datetime.now()
                        - datetime.timedelta(
                            minutes=random.randint(1, cfg.traffic_lookback_minutes)
                        )
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                    "is_ground_truth_anomaly": False,
                }
            )

    # Random connections with anomaly injection.
    #
    # Anomaly injection is a pure random roll, independent of whether `src`
    # is a known threat IP. Earlier this was gated on `src in threat_ips`,
    # which made the label perfectly recoverable from a static 3-IP list —
    # a detector didn't need to learn anything about traffic volume, just
    # membership in EXTERNAL_THREAT_IPS. Decoupling this is what makes
    # detector benchmarks against this generator mean anything (see
    # PLAN_MASTER.md Phase 1, milestone M1.5). EXTERNAL_THREAT_IPS entries
    # remain ordinary possible src/dst values — real anomalies are no
    # longer confined to them.
    for _ in range(num_edges):
        src = random.choice(valid_ips)
        dst = random.choice(valid_ips)
        while dst == src:
            dst = random.choice(valid_ips)

        duration = float(np.random.exponential(scale=cfg.normal_duration_scale))
        packet_count = int(np.random.exponential(scale=cfg.normal_packet_scale)) + cfg.normal_packet_offset
        bytes_transferred = int(
            packet_count * np.random.normal(loc=cfg.normal_bytes_per_packet_mean, scale=cfg.normal_bytes_per_packet_std)
        )

        is_ground_truth_anomaly = random.random() < anomaly_rate
        if is_ground_truth_anomaly:
            duration = float(np.random.uniform(cfg.anomaly_duration_min_sec, cfg.anomaly_duration_max_sec))
            bytes_transferred = int(
                np.random.uniform(cfg.anomaly_bytes_min_mb, cfg.anomaly_bytes_max_mb) * 1024 * 1024
            )
            packet_count = int(np.random.uniform(cfg.anomaly_packets_min, cfg.anomaly_packets_max))

        edges.append(
            {
                "source": src,
                "target": dst,
                "duration_sec": round(duration, 3),
                "packets": packet_count,
                "bytes": abs(bytes_transferred),
                "timestamp": (
                    datetime.datetime.now()
                    - datetime.timedelta(
                        minutes=random.randint(1, cfg.traffic_lookback_minutes)
                    )
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "is_ground_truth_anomaly": is_ground_truth_anomaly,
            }
        )

    edges_df = pd.DataFrame(edges)
    return nodes_df, edges_df


def generate_scripted_attack(
    attack_name: str,
    exfil_edge: dict,
    recon_delay_sec: int | None = None,
    base_timestamp: datetime.datetime | None = None,
    seed: int | None = None,
) -> list[dict]:
    """Build the two-stage event list for one scripted attack preset (Phase 2).

    A recon stage — the attacker uses the target Purdue zone's honeytoken
    credential (deterministic tripwire, fires at t=0) — precedes the
    existing exfiltration/lateral-movement step (fires at
    t=recon_delay_sec once volume crosses the ML detector's threshold). This
    gives a measurable lead-time delta: the tripwire is expected to fire
    `recon_delay_sec` seconds before the volumetric detector would.

    Parameters
    ----------
    attack_name:
        One of the keys in ATTACK_RECON_GATEWAY (the four scripted attack
        presets in aegis_demo.py's sidebar).
    exfil_edge:
        The existing attack_edges-shaped dict for the exfil/lateral-movement
        step (keys: source, target, duration_sec, packets, bytes, timestamp).
        Its `timestamp` is overwritten with `base_timestamp + recon_delay_sec`.
    recon_delay_sec:
        Seconds between recon and exfil. Defaults to SETTINGS.deception.recon_delay_sec.
    base_timestamp:
        Recon event time (t=0). Defaults to now (UTC).
    seed:
        Random seed for the recon event's timing jitter. Defaults to
        SETTINGS.deception.random_seed.

    Returns
    -------
    list[dict]
        [recon_event, exfil_event], both shaped like the `attack_edges` list
        core/pipeline.run_analysis() concatenates onto edges_df. The recon
        event additionally carries `is_honeytoken_use=True` and
        duration_sec=packets=bytes=0 (never fabricated — contract C4);
        core/pipeline.py's tripwire fusion reads that column.

    Raises
    ------
    ValueError
        If `attack_name` has no entry in ATTACK_RECON_GATEWAY.
    """
    gateway_zone = ATTACK_RECON_GATEWAY.get(attack_name)
    if gateway_zone is None:
        raise ValueError(
            f"No recon gateway mapped for attack {attack_name!r}. "
            f"Expected one of {sorted(ATTACK_RECON_GATEWAY)}."
        )

    cfg = SETTINGS.deception
    delay = recon_delay_sec if recon_delay_sec is not None else cfg.recon_delay_sec
    seed = seed if seed is not None else cfg.random_seed
    base_timestamp = base_timestamp or datetime.datetime.now(datetime.timezone.utc)

    from deception.adapter import generate_tripwire_events

    recon_df = generate_tripwire_events(
        gateway_zone, count=1, seed=seed, base_timestamp=base_timestamp,
    )
    recon_row = recon_df.iloc[0]
    recon_event = {
        "source": recon_row["source_asset_id"],
        "target": recon_row["destination_asset_id"],
        "duration_sec": float(recon_row["duration_sec"]),
        "packets": int(recon_row["packets"]),
        "bytes": float(recon_row["bytes"]),
        "timestamp": recon_row["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
        "is_honeytoken_use": bool(recon_row["is_honeytoken_use"]),
    }

    exfil_timestamp = base_timestamp + datetime.timedelta(seconds=delay)
    exfil_event = dict(exfil_edge)
    exfil_event["timestamp"] = exfil_timestamp.strftime("%Y-%m-%d %H:%M:%S")
    exfil_event.setdefault("is_honeytoken_use", False)

    return [recon_event, exfil_event]
