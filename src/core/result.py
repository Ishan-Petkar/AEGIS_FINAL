"""
result.py — AnalysisResult, the single output type of run_analysis()
(Phase 1, contract C1).

The dashboard reads only this object — it never touches ml_engine, the CII
engine, or graph_manager directly. That boundary is what makes it possible to
swap the UI for an API response (Phase 5) without touching analysis logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from cii_calculator import CIIResult


@dataclass
class AnalysisResult:
    """Everything the dashboard (or a future API handler) needs to render
    one analysis pass: scored traffic, asset lookups, and — if an attack or
    what-if scenario is active — the Monte Carlo cascading-impact result.
    """

    nodes_df: pd.DataFrame
    edges_df: pd.DataFrame
    ip_to_asset: dict
    criticality_map: dict

    total_connections: int
    anomalous_connections: int
    anomaly_pct: float

    cii: CIIResult
    network_risk: int

    #: Set when a requested real dataset couldn't be loaded (e.g. missing
    #: files on disk) — the UI shows this as a warning instead of crashing.
    dataset_warning: Optional[str] = None
