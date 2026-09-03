"""
backend/warmup_supervised.py — Phase B improvement pass: `python -m
backend.warmup_supervised` build-time CLI.

Fits a `SupervisedFlowScorer` (the KNOWN-THREAT channel,
`backend/supervised_detector.py`) on the first
`BACKEND_SETTINGS.supervised_train_split_fraction` of
`BACKEND_SETTINGS.replay_default_dataset_day` and persists it to
`BACKEND_SETTINGS.supervised_model_artifact_path_resolved`. Mirrors
`backend/warmup.py` exactly, for the second detector artifact.

Unlike `backend.warmup`, running this is OPTIONAL: `backend.runtime.
build_runtime()` catches a missing artifact and starts the API with the
known-threat channel simply absent (two live channels instead of three) —
see `SupervisedFlowScorerArtifactMissing`'s docstring.

Usage
-----
    PYTHONPATH=src venv/bin/python -m backend.warmup_supervised
"""
from __future__ import annotations

import logging
import time

from backend.supervised_detector import SupervisedFlowScorer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    fit_start = time.perf_counter()
    scorer = SupervisedFlowScorer()
    scorer.fit_from_warmup()
    fit_elapsed = time.perf_counter() - fit_start

    save_start = time.perf_counter()
    artifact_path = scorer.save()
    save_elapsed = time.perf_counter() - save_start

    warmup_meta = scorer.baseline["warmup"]
    hyperparameters = scorer.baseline["hyperparameters"]
    artifact_size_bytes = artifact_path.stat().st_size

    print("SupervisedFlowScorer warmup build complete.")
    print(f"  day:                    {warmup_meta['day']}")
    print(f"  source_file:            {warmup_meta['source_file']}")
    print(f"  split_fraction:         {warmup_meta['split_fraction']}")
    print(f"  rows_used (train):      {warmup_meta['rows_used']}")
    print(f"  attack_rows_in_training:{warmup_meta['attack_rows_in_training']}")
    print(f"  n_estimators:           {hyperparameters['n_estimators']}")
    print(f"  fit time:               {fit_elapsed:.3f}s")
    print(f"  save time:              {save_elapsed:.3f}s")
    print(f"  artifact path:          {artifact_path}")
    print(f"  artifact size:          {artifact_size_bytes / 1024:.1f} KiB")
    print()
    print(
        "Reminder: this detector has genuinely seen the labels for the "
        "FIRST half of this day during training (see SupervisedFlowScorer's "
        "class docstring) -- its honestly-measured performance (AUC 0.847, "
        "precision 0.996, recall 0.595, docs/DETECTION_STUDY.md Test 1) "
        "describes the SECOND half, which it has not seen."
    )


if __name__ == "__main__":
    main()
