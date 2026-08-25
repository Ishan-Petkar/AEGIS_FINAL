"""
backend/warmup.py — Phase 5 Ticket #5: `python -m backend.warmup` build-time CLI.

Fits a `StreamingScorer` on the benign warmup day
(`BACKEND_SETTINGS.warmup_dataset_day`, "monday") and persists it to
`BACKEND_SETTINGS.model_artifact_path_resolved`. Measured cost on this
machine: 4.72s end-to-end for the full ~529,918-row Monday warmup
(docs/PHASE5_TICKET5_PLAN.md section 2, Measurement B) — cheap enough to
run as a normal build/deploy step, not something the demo does at request
time. This satisfies docs/PHASE5_TICKET5_PLAN.md section 7's requirement
that a missing artifact fail fast (`StreamingScorerArtifactMissing`) rather
than silently fit on stream data (Invariant B).

Usage
-----
    PYTHONPATH=src venv/bin/python -m backend.warmup
"""
from __future__ import annotations

import logging
import time

from backend.streaming import StreamingScorer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    fit_start = time.perf_counter()
    scorer = StreamingScorer()
    scorer.fit_from_warmup()
    fit_elapsed = time.perf_counter() - fit_start

    save_start = time.perf_counter()
    artifact_path = scorer.save()
    save_elapsed = time.perf_counter() - save_start

    baseline = scorer.baseline
    warmup_meta = baseline["warmup"]
    hyperparameters = baseline["hyperparameters"]
    artifact_size_bytes = artifact_path.stat().st_size

    print("StreamingScorer warmup build complete.")
    print(f"  day:                   {warmup_meta['day']}")
    print(f"  source_file:           {warmup_meta['source_file']}")
    print(f"  rows_used:             {warmup_meta['rows_used']}")
    print(f"  attack_rows_in_warmup: {warmup_meta['attack_rows_in_warmup']}")
    print(f"  contamination:         {hyperparameters['contamination']}")
    print(f"  n_estimators:          {hyperparameters['n_estimators']}")
    print(f"  fit time:              {fit_elapsed:.3f}s")
    print(f"  save time:             {save_elapsed:.3f}s")
    print(f"  artifact path:         {artifact_path}")
    print(f"  artifact size:         {artifact_size_bytes / 1024:.1f} KiB")


if __name__ == "__main__":
    main()
