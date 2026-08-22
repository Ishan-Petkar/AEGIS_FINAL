"""
settings.py — Canonical, typed configuration for AEGIS.

Every numeric parameter (decay rate, thresholds, criticality defaults,
model hyperparameters) lives here. No module should use a hardcoded literal
for any of these values; import from this module instead.

Usage:
    from settings import SETTINGS
    print(SETTINGS.bfs_decay_rate)
"""

from pydantic import BaseModel, Field
from typing import ClassVar


class CIISettings(BaseModel):
    """Settings controlling the Cascading Impact Index computation."""

    bfs_max_hops: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum BFS traversal depth from an anomalous node.",
    )
    bfs_decay_rate: float = Field(
        default=0.7,
        gt=0.0,
        lt=1.0,
        description=(
            "Per-hop probability decay factor. At hop h, the cumulative "
            "probability is multiplied by decay_rate^h. Lower values attenuate "
            "distant impact faster."
        ),
    )
    default_criticality: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Fallback criticality weight used when an asset is not present "
            "in the criticality map."
        ),
    )
    cii_max_value: float = Field(
        default=1.0,
        gt=0.0,
        description="Upper bound that CII scores are clamped to.",
    )
    mc_iterations: int = Field(
        default=1000,
        ge=100,
        le=100_000,
        description=(
            "Number of Monte Carlo simulation iterations for probabilistic "
            "risk propagation. Higher values give tighter confidence intervals "
            "but increase computation time."
        ),
    )


class MLSettings(BaseModel):
    """Settings controlling the ML anomaly detection engine."""

    isolation_forest_n_estimators: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Number of trees in the Isolation Forest.",
    )
    isolation_forest_contamination: float = Field(
        default=0.08,
        gt=0.0,
        lt=0.5,
        description=(
            "Expected proportion of anomalies in training data "
            "(IsolationForest 'contamination' parameter)."
        ),
    )
    isolation_forest_random_state: int = Field(
        default=42,
        description="Random seed for reproducibility.",
    )
    default_features: list[str] = Field(
        default=["duration_sec", "packets", "bytes"],
        description="Feature columns used by the ML engine by default.",
    )


class DataGenSettings(BaseModel):
    """Settings controlling synthetic data generation."""

    num_nodes: int = Field(default=30, ge=1)
    num_edges: int = Field(default=60, ge=1)
    anomaly_rate: float = Field(default=0.15, ge=0.0, le=1.0)
    random_seed: int = Field(default=42)

    anomaly_duration_min_sec: float = Field(default=15.0, gt=0.0)
    anomaly_duration_max_sec: float = Field(default=120.0, gt=0.0)
    anomaly_bytes_min_mb: float = Field(default=50.0, gt=0.0)
    anomaly_bytes_max_mb: float = Field(default=500.0, gt=0.0)
    anomaly_packets_min: int = Field(default=10_000, ge=1)
    anomaly_packets_max: int = Field(default=50_000, ge=1)

    normal_duration_scale: float = Field(default=1.5, gt=0.0)
    normal_packet_scale: float = Field(default=100.0, gt=0.0)
    normal_packet_offset: int = Field(default=5, ge=0)
    normal_bytes_per_packet_mean: float = Field(default=1024.0, gt=0.0)
    normal_bytes_per_packet_std: float = Field(default=256.0, gt=0.0)
    baseline_repeat_count: int = Field(default=3, ge=1)
    traffic_lookback_minutes: int = Field(default=60, ge=1)


class GatewaySettings(BaseModel):
    """Settings controlling the mandatory access-gateway topology (Phase 1, contract C2).

    Every asset at/above `criticality_threshold` becomes "protected": graph_manager
    rewrites any inbound edge so it routes through the asset's Purdue-zone gateway
    node instead of terminating on the asset directly. See PLAN_MASTER.md Phase 1.
    """

    criticality_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Assets with criticality >= this value are 'protected' — no inbound "
            "edge may terminate on them directly."
        ),
    )
    gateway_node_criticality: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Criticality assigned to gateway nodes themselves. Kept near-zero so "
            "a gateway hit is pure detection signal and does not distort Cascading "
            "Impact Index totals (Decision #4 in PLAN_MASTER.md)."
        ),
    )


class DeceptionSettings(BaseModel):
    """Settings controlling the Phase 2 honeytoken tripwire detector and its
    fusion with the volumetric (Isolation Forest) detector.

    See src/deception/tripwire.py (detector), src/deception/adapter.py
    (event generation), and core/pipeline.py (fusion) for consumers.
    """

    recon_delay_sec: int = Field(
        default=60,
        ge=0,
        le=86_400,
        description=(
            "Seconds between a scripted attack's recon stage (tripwire fires "
            "at t=0) and its exfil stage (volumetric detector fires at "
            "t=recon_delay_sec). Creates a measurable lead-time delta."
        ),
    )
    random_seed: int = Field(
        default=42,
        description="Seed for tripwire event generation timing jitter.",
    )
    tripwire_score: float = Field(
        default=-100.0,
        le=0.0,
        description=(
            "score_samples() value TripwireDetector assigns a honeytoken hit "
            "— deliberately far below any IsolationForest raw_score so it "
            "always reads as maximally anomalous under the shared sklearn "
            "convention (lower = more anomalous)."
        ),
    )
    confidence_both: float = Field(
        default=0.99,
        ge=0.0,
        le=1.0,
        description=(
            "Fused confidence when both the tripwire and the volumetric "
            "detector fire on the same connection — two independent signals "
            "escalate certainty."
        ),
    )
    confidence_tripwire_only: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence when only the tripwire fires. High: a honeytoken has "
            "zero legitimate use anywhere in the system, so this signal alone "
            "is already strong evidence of compromise."
        ),
    )
    confidence_volume_only: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence when only the volumetric detector fires (no "
            "honeytoken involved) — the pre-Phase-2 baseline confidence."
        ),
    )
    confidence_none: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence when neither signal fires on a connection.",
    )
    tripwire_gateway_breach_prob: float = Field(
        default=0.9,
        gt=0.0,
        le=1.0,
        description=(
            "Propagation probability used for the synthetic gateway -> "
            "guarded-asset relay edge cii_calculator splices in when a "
            "tripwire fires on a gateway that has no materialised "
            "DEPENDENCY_GRAPH edge yet (e.g. deception-only telemetry with "
            "no exfiltration recorded)."
        ),
    )
    tripwire_zone_breach_criticality: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum criticality credited to a tripwire hit on a gateway "
            "zone that currently guards no protected asset in "
            "DEPENDENCY_GRAPH. A honeytoken match is unambiguous compromise "
            "even when nothing downstream is modelled yet, so CII must never "
            "silently read as zero for it."
        ),
    )


class EvaluationSettings(BaseModel):
    """Settings controlling the Phase 3 evaluation harness (src/evaluation/).

    See src/evaluation/__init__.py's DegenerateEvaluationError: the old
    behavior silently reported P=0.000 R=0.000 F1=0.000 AUC=nan whenever an
    eval split happened to be (near-)100% one class. These bounds define
    "degenerate" precisely so that condition raises instead of reporting a
    fake result.
    """

    min_positive_rate: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum fraction of eval-set rows that must be true anomalies. "
            "Below this, precision/recall/F1/AUC are considered too "
            "statistically unreliable to report — run_evaluation() raises "
            "DegenerateEvaluationError instead of returning zeros."
        ),
    )
    max_positive_rate: float = Field(
        default=0.99,
        ge=0.0,
        le=1.0,
        description=(
            "Maximum fraction of eval-set rows that may be true anomalies "
            "before the split is considered degenerate (e.g. an all-attack "
            "source with no benign traffic to discriminate against)."
        ),
    )


class AEGISSettings(BaseModel):
    """Top-level settings object aggregating all sub-settings."""

    cii: CIISettings = Field(default_factory=CIISettings)
    ml: MLSettings = Field(default_factory=MLSettings)
    data_gen: DataGenSettings = Field(default_factory=DataGenSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    deception: DeceptionSettings = Field(default_factory=DeceptionSettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)

    # Singleton access pattern — instantiated once at module load.
    _instance: ClassVar["AEGISSettings | None"] = None

    model_config = {"frozen": True}  # Immutable after construction


# Module-level singleton — import this everywhere.
SETTINGS = AEGISSettings()
