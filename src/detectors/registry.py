"""
registry.py — Detector name -> class lookup table (Phase 1, contract C3).

Phase 3's evaluation harness iterates this registry instead of hardcoding a
detector list, so a new detector (Phase 2's tripwire, Phase 4's graph-aware
models) is available for benchmarking the moment it's registered here.
"""
from __future__ import annotations

from deception.tripwire import TripwireDetector
from detectors.sklearn_wrappers import IsolationForestDetector, OneClassSVMDetector
from detectors.statistical import MADDetector, ZScoreDetector

#: name -> BaseDetector subclass, constructible with no required arguments
DETECTORS: dict[str, type] = {
    "isolation_forest": IsolationForestDetector,
    "zscore": ZScoreDetector,
    "mad": MADDetector,
    "ocsvm": OneClassSVMDetector,
    "tripwire": TripwireDetector,
}


def register_detector(name: str, detector_cls: type) -> None:
    """Register an additional detector class under `name`."""
    DETECTORS[name] = detector_cls
