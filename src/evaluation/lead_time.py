"""
lead_time.py — Tripwire vs. volumetric detection lead-time measurement
(AEGIS Phase 3).

Phase 2's thesis is that a honeytoken tripwire at a Purdue-zone gateway
fires meaningfully *earlier* than the old volume-based (Isolation Forest /
Z-Score / MAD) detector, because it catches the recon stage of an attack
instead of waiting for the exfiltration/lateral-movement stage to look
anomalous by volume. This module makes that a measured number instead of a
claim: it replays each of the four scripted-attack presets
(data_generator.ATTACK_RECON_GATEWAY) through data_generator's own two-stage
timeline builder (generate_scripted_attack) and computes the delta between
the tripwire's detection instant and the volumetric detector's.

Deliberately NOT part of the run_evaluation() precision/recall/F1/AUC loop
in evaluation/__init__.py: TripwireDetector's only feature is
is_honeytoken_use, which is absent (implicitly False) on every ordinary
CIC-IDS2017/PaySim/SWaT row, so running it through that harness would
trivially predict "normal" for the entire dataset — a meaningless result,
not a real one. Lead time is tripwire's own metric, measured on its own
timeline, exactly the two-event recon-then-exfil sequence it exists to win.

Detection-instant convention
-----------------------------
Tripwire side: deception/adapter.generate_tripwire_events sets
``observed_at`` = event timestamp + a modeled ~1s log/alert latency
(_DETECTION_LATENCY_SEC) — this is the tripwire's actual detection instant,
not just when the probe occurred.

Volumetric side: the codebase has no analogous alerting-latency model for
the Isolation Forest / statistical detectors — ml_engine.compute_anomaly_scores
scores a batch, it does not simulate streaming/alerting delay. This module
therefore credits the volumetric detector with the most generous possible
detection instant available: the moment the exfil connection itself lands
(base_timestamp + recon_delay_sec, exactly the exfil event's own
timestamp). Any real deployment would add scoring/alerting latency on top
of that, so this is a conservative, pro-baseline comparison — if anything
it UNDERSTATES the tripwire's true lead-time advantage, never overstates it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from data_generator import ATTACK_RECON_GATEWAY, generate_scripted_attack
from deception.adapter import generate_tripwire_events
from settings import SETTINGS

#: The exact exfil-edge dicts the dashboard's sidebar attack buttons use
#: (src/aegis_demo.py). Duplicated here (rather than imported from the UI
#: module) so this module stays importable with zero Streamlit dependency —
#: see the "no Streamlit imports in evaluation/" constraint. If a button's
#: edge dict changes in aegis_demo.py, update it here too.
SCRIPTED_ATTACK_EXFIL_EDGES: dict[str, dict] = {
    "Payment Gateway Breach": {
        "source": "10.0.1.20", "target": "198.51.100.42",
        "duration_sec": 120.0, "packets": 100_000, "bytes": 500_000_000,
    },
    "Camera Spoofing": {
        "source": "10.0.1.10", "target": "10.0.1.12",
        "duration_sec": 100.0, "packets": 50_000, "bytes": 500_000_000,
    },
    "Data Exfiltration": {
        "source": "10.0.1.16", "target": "45.227.254.12",
        "duration_sec": 300.0, "packets": 150_000, "bytes": 1_500_000_000,
    },
    "Lateral Movement": {
        "source": "10.0.1.15", "target": "10.0.1.13",
        "duration_sec": 45.0, "packets": 20_000, "bytes": 20_000_000,
    },
}


@dataclass(frozen=True)
class LeadTimeResult:
    """Lead-time measurement for one scripted attack."""

    attack_name: str
    gateway_zone: str
    recon_detected_at: datetime
    exfil_detected_at: datetime
    lead_time_seconds: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["recon_detected_at"] = self.recon_detected_at.isoformat()
        d["exfil_detected_at"] = self.exfil_detected_at.isoformat()
        return d


def compute_lead_time(
    attack_name: str,
    exfil_edge: Optional[dict] = None,
    recon_delay_sec: Optional[int] = None,
    base_timestamp: Optional[datetime] = None,
    seed: Optional[int] = None,
) -> LeadTimeResult:
    """Compute the tripwire-vs-volumetric lead time for one scripted attack.

    Parameters
    ----------
    attack_name:
        One of data_generator.ATTACK_RECON_GATEWAY's keys (the four
        dashboard attack presets).
    exfil_edge:
        The attack_edges-shaped exfil dict (as passed to
        generate_scripted_attack). Defaults to SCRIPTED_ATTACK_EXFIL_EDGES[attack_name].
    recon_delay_sec:
        Seconds between recon and exfil. Defaults to SETTINGS.deception.recon_delay_sec.
    base_timestamp:
        Recon event time (t=0). Defaults to now (UTC).
    seed:
        Random seed for recon timing jitter. Defaults to SETTINGS.deception.random_seed.

    Returns
    -------
    LeadTimeResult

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
    if exfil_edge is None:
        exfil_edge = SCRIPTED_ATTACK_EXFIL_EDGES[attack_name]

    cfg = SETTINGS.deception
    delay = recon_delay_sec if recon_delay_sec is not None else cfg.recon_delay_sec
    seed = seed if seed is not None else cfg.random_seed
    base_timestamp = base_timestamp or datetime.now(timezone.utc)

    # Replay the actual production two-stage builder — this doubles as an
    # integration check that the dashboard's own attack-preset timeline
    # still produces two valid events for this attack.
    generate_scripted_attack(
        attack_name, exfil_edge, recon_delay_sec=delay,
        base_timestamp=base_timestamp, seed=seed,
    )

    # generate_scripted_attack's public recon_event dict keeps only
    # "timestamp" (the edges_df-compatible field); it drops `observed_at`
    # (the C4 field marking the tripwire's actual detection instant,
    # timestamp + deception/adapter._DETECTION_LATENCY_SEC) because
    # edges_df/core.pipeline has no use for it. Recover it by calling the
    # same generator with matching (gateway_zone, seed, base_timestamp)
    # rather than widen generate_scripted_attack's contract for a field
    # only this module needs.
    recon_df = generate_tripwire_events(
        gateway_zone, count=1, seed=seed, base_timestamp=base_timestamp,
    )
    recon_detected_at = recon_df.iloc[0]["observed_at"]
    if not isinstance(recon_detected_at, datetime):
        recon_detected_at = recon_detected_at.to_pydatetime()

    # See module docstring: most generous possible instant for the baseline.
    exfil_detected_at = base_timestamp + timedelta(seconds=delay)

    lead_time_seconds = (exfil_detected_at - recon_detected_at).total_seconds()

    return LeadTimeResult(
        attack_name=attack_name,
        gateway_zone=gateway_zone,
        recon_detected_at=recon_detected_at,
        exfil_detected_at=exfil_detected_at,
        lead_time_seconds=lead_time_seconds,
    )


def compute_all_scripted_attack_lead_times(
    recon_delay_sec: Optional[int] = None,
    base_timestamp: Optional[datetime] = None,
    seed: Optional[int] = None,
) -> list[LeadTimeResult]:
    """Compute lead time for all four scripted attack presets.

    Returns
    -------
    list[LeadTimeResult]
        One entry per key in data_generator.ATTACK_RECON_GATEWAY.
    """
    return [
        compute_lead_time(
            name, recon_delay_sec=recon_delay_sec,
            base_timestamp=base_timestamp, seed=seed,
        )
        for name in ATTACK_RECON_GATEWAY
    ]


def summarize_lead_times(results: list[LeadTimeResult]) -> dict:
    """Summarize a batch of LeadTimeResult: how many attacks the tripwire won.

    Returns
    -------
    dict
        ``n_attacks``, ``n_tripwire_earlier`` (lead_time_seconds > 0),
        ``mean_lead_time_seconds``.
    """
    n = len(results)
    n_earlier = sum(1 for r in results if r.lead_time_seconds > 0)
    mean_lead = (sum(r.lead_time_seconds for r in results) / n) if n else float("nan")
    return {
        "n_attacks": n,
        "n_tripwire_earlier": n_earlier,
        "mean_lead_time_seconds": mean_lead,
    }
