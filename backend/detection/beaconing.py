"""
backend/detection/beaconing.py — the temporal/periodicity detector.

Why this detector exists
-------------------------------------------------------------------------
docs/DETECTION_STUDY.md's measured finding is that Bot C2 beacons are
SMALLER than benign traffic (median 6 bytes vs 70), not larger. Every
volumetric detector in this codebase (`StreamingScorer`'s Isolation
Forest, the z-score/MAD baselines) looks for outliers in size/duration/
packet-count space, so a beacon that is quietly small sits INSIDE the
benign distribution rather than at its tail — the volumetric channel is
not merely weak against this traffic, it is structurally blind to it,
because "small and frequent" is not a shape the feature set can express.

What a beacon cannot hide is its rhythm: a C2 implant calling home every
N seconds is regular in a way human-driven traffic is not. That regularity
lives entirely in inter-arrival TIMING between flows from the same
(source_ip, destination_ip) pair — a dimension none of the volumetric
features touch. `BeaconingDetector` is the channel built to see it.

Algorithm
-------------------------------------------------------------------------
For each tracked `(source_ip, destination_ip)` pair (`FlowFeatures.
pair_key` — ports excluded on purpose, see that property's docstring),
maintain a bounded ring buffer of recent flow timestamps. On each new
flow: append its `ts`, take consecutive differences to get inter-arrival
intervals, discard intervals outside `[beaconing_min_interval_sec,
beaconing_max_interval_sec]`, and compute the coefficient of variation
(CV = stdev / mean) of what remains. Low CV means metronomic timing;
bursty human-driven traffic scores far higher. A pair fires when
`cv <= beaconing_max_cv`.

The `beaconing_min_interval_sec` floor is not a tuning nicety — it is
load-bearing. `BackendSettings.beaconing_min_interval_sec`'s docstring
records that CIC-IDS2017's friday-morning peak carries 4,017 rows sharing
one minute-granularity timestamp; without the floor, that burst alone
manufactures near-zero intervals with near-zero variance, i.e. a perfect
fake beacon assembled entirely out of the dataset's timestamp resolution
rather than any real periodicity. Filtering those intervals out before
computing CV is what keeps this detector honest about what it actually
observed.

Below `beaconing_min_samples` flows for a pair (equivalently, fewer than
`beaconing_min_samples - 1` USABLE intervals after the floor/ceiling
filter), the sample is too small for a CV to mean anything and the
detector abstains rather than guessing: `fired=False,
calibrated_score=0.0`, with the reason recorded in `evidence`.

State and bounds
-------------------------------------------------------------------------
Per-pair history is a `collections.deque(maxlen=beaconing_history_per_pair)`
so memory per pair is bounded and the CV window stays recent — a beacon
that started an hour ago is judged on its current rhythm, not its whole
lifetime. The set of tracked pairs is itself bounded: an `OrderedDict`
capped at `beaconing_max_tracked_pairs`, evicted least-recently-used, the
same pattern `backend/ingest.py` uses for `_cii_cache` / `_last_alert_at`
(`move_to_end` on touch, `popitem(last=False)` to evict). This is not
theoretical — risk T5 records that `AssetRegistry` auto-registers one
`Unresolved_<ip>` asset per unique unresolved IP, and a real replay day
carries hundreds of distinct /24s worth of source addresses, so unbounded
per-pair state is an unbounded leak over a long-running stream.

Calibration
-------------------------------------------------------------------------
`calibrated_score = clamp(1.0 - cv / beaconing_max_cv, 0.0, 1.0)`, applied
uniformly whether or not the pair fires. This is a straight linear map of
CV onto [0, 1]: `cv == 0` (perfectly periodic) scores 1.0, `cv ==
beaconing_max_cv` (right at the fire/no-fire boundary) scores 0.0, and
anything less regular than the threshold clamps to 0.0 rather than going
negative. It is monotonically DECREASING in `cv` by construction (higher
variation, lower score), continuous across the fire boundary (no cliff at
the threshold), and always within `[0, 1]` as `DetectorVerdict.
__post_init__` requires. Like `hybrid_weight_beaconing` itself, this
mapping is a principled placeholder, not a measured calibration — there is
no labelled beacon corpus in this project yet to fit it against, and the
evidence payload always carries the raw `cv` and the threshold it was
compared to so a reviewer can re-derive the decision by hand.

`certainty` is always `Certainty.HEURISTIC`: a regular inter-arrival
rhythm is consistent with a beacon but is also consistent with, say, a
health-check or a cron job, so this channel must never be allowed to
confirm anything on its own (mirrors the tripwire/heuristic split
`Certainty`'s docstring lays out).
"""

from __future__ import annotations

import statistics
from collections import OrderedDict, deque
from typing import Deque, Optional, Sequence

from backend.config import BACKEND_SETTINGS
from backend.detection.contracts import (
    DETECTOR_BEACONING,
    Certainty,
    DetectorVerdict,
    FlowFeatures,
)

# ---------------------------------------------------------------------------
# BeaconingDetector
# ---------------------------------------------------------------------------


class BeaconingDetector:
    """Temporal/periodicity detector — see the module docstring for why.

    Stateful and batch-oriented per the `FlowDetector` protocol
    (`backend/detection/contracts.py`): `examine()` updates per-pair
    inter-arrival history as it goes, so verdicts on later flows in the
    same batch can reflect earlier flows in that same batch. Deliberately
    NOT registered in `src/detectors/registry.py` — see the
    `FlowDetector` protocol docstring (K6) for why registration there
    would silently enrol this detector in the offline benchmark loop it
    was never designed for.
    """

    #: Stable name written to `event_scores.detector` — part of the
    #: `FlowDetector` protocol contract.
    name: str = DETECTOR_BEACONING

    def __init__(
        self,
        min_samples: Optional[int] = None,
        history_per_pair: Optional[int] = None,
        max_tracked_pairs: Optional[int] = None,
        max_cv: Optional[float] = None,
        min_interval_sec: Optional[float] = None,
        max_interval_sec: Optional[float] = None,
        reliability: Optional[float] = None,
    ) -> None:
        # Optional-override convention (CLAUDE.md section 5): every
        # tunable falls back to BACKEND_SETTINGS inside the body, never a
        # bare literal, so tests/sweeps can override without mutating the
        # frozen settings singleton.
        self._min_samples = (
            min_samples if min_samples is not None else BACKEND_SETTINGS.beaconing_min_samples
        )
        self._history_per_pair = (
            history_per_pair
            if history_per_pair is not None
            else BACKEND_SETTINGS.beaconing_history_per_pair
        )
        self._max_tracked_pairs = (
            max_tracked_pairs
            if max_tracked_pairs is not None
            else BACKEND_SETTINGS.beaconing_max_tracked_pairs
        )
        self._max_cv = max_cv if max_cv is not None else BACKEND_SETTINGS.beaconing_max_cv
        self._min_interval_sec = (
            min_interval_sec
            if min_interval_sec is not None
            else BACKEND_SETTINGS.beaconing_min_interval_sec
        )
        self._max_interval_sec = (
            max_interval_sec
            if max_interval_sec is not None
            else BACKEND_SETTINGS.beaconing_max_interval_sec
        )
        self._reliability = (
            reliability if reliability is not None else BACKEND_SETTINGS.hybrid_weight_beaconing
        )

        # LRU-bounded per-pair timestamp history. Same shape as
        # `IngestPipeline._cii_cache` / `_last_alert_at`
        # (backend/ingest.py): `OrderedDict[pair_key, deque[timestamps]]`,
        # `move_to_end` on every touch, `popitem(last=False)` to evict the
        # least-recently-used pair once the cap is exceeded.
        self._history: "OrderedDict[tuple[str, str], Deque]" = OrderedDict()

    # -----------------------------------------------------------------
    # FlowDetector protocol
    # -----------------------------------------------------------------

    def examine(self, flows: Sequence[FlowFeatures]) -> list[DetectorVerdict]:
        """Return exactly one verdict per input flow, in input order.

        Order and length are contractual (see `FlowDetector.examine`'s
        docstring) — `backend/ingest.py` zips verdicts against
        `inserted_ids` positionally.
        """
        verdicts: list[DetectorVerdict] = []
        for flow in flows:
            history = self._touch(flow.pair_key)
            history.append(flow.ts)
            verdicts.append(self._verdict_for(history))
        return verdicts

    def reset(self) -> None:
        """Clear all per-pair history. Used by tests and by any future
        session-boundary reset (a new replay run starting fresh)."""
        self._history.clear()

    @property
    def tracked_pairs(self) -> int:
        """Number of `(source_ip, destination_ip)` pairs currently held in
        history. Exposed for tests and telemetry — a cheap way to observe
        the LRU cap actually holding under load."""
        return len(self._history)

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _touch(self, pair_key: tuple) -> Deque:
        """Return the ring buffer for `pair_key`, creating it if new and
        applying LRU eviction — mirrors `IngestPipeline`'s bounded
        `OrderedDict` pattern in `backend/ingest.py` exactly (touch via
        `move_to_end`, evict the oldest via `popitem(last=False)`)."""
        if pair_key in self._history:
            self._history.move_to_end(pair_key)
        else:
            self._history[pair_key] = deque(maxlen=self._history_per_pair)
            self._history.move_to_end(pair_key)
            while len(self._history) > self._max_tracked_pairs:
                self._history.popitem(last=False)
        return self._history[pair_key]

    def _verdict_for(self, history: Deque) -> DetectorVerdict:
        """Compute this pair's verdict from its current timestamp history.

        `history` already includes the flow being scored (appended by the
        caller before this runs), matching the module docstring's stated
        order: append, then compute intervals from the stored history.
        """
        timestamps = list(history)
        # abs() guards against out-of-order timestamps within a batch
        # (clock skew, replay reordering) producing a spurious negative
        # interval — a beacon's rhythm is about the MAGNITUDE of the gap
        # between calls, not its sign.
        raw_intervals = [
            abs((timestamps[i] - timestamps[i - 1]).total_seconds())
            for i in range(1, len(timestamps))
        ]
        usable = [
            interval
            for interval in raw_intervals
            if self._min_interval_sec <= interval <= self._max_interval_sec
        ]
        usable_count = len(usable)
        required = self._min_samples - 1

        if usable_count < required:
            return DetectorVerdict(
                detector=self.name,
                fired=False,
                calibrated_score=0.0,
                reliability=self._reliability,
                certainty=Certainty.HEURISTIC,
                raw_score=None,
                evidence={
                    "abstained": "insufficient_history",
                    "usable_intervals": usable_count,
                    "required_intervals": required,
                    "total_intervals_observed": len(raw_intervals),
                    "min_interval_sec": self._min_interval_sec,
                    "max_interval_sec": self._max_interval_sec,
                },
            )

        mean_interval = statistics.mean(usable)
        # `usable_count >= required >= 2` whenever we reach here
        # (`beaconing_min_samples >= 3` is enforced by BackendSettings'
        # field bound), so a sample stdev over >= 2 points is always
        # well-defined.
        stdev_interval = statistics.stdev(usable)
        cv = stdev_interval / mean_interval

        fired = cv <= self._max_cv
        # See module docstring "Calibration": linear, monotonically
        # decreasing map of cv onto [0, 1], clamped at both ends.
        calibrated_score = max(0.0, min(1.0, 1.0 - (cv / self._max_cv)))

        return DetectorVerdict(
            detector=self.name,
            fired=fired,
            calibrated_score=calibrated_score,
            reliability=self._reliability,
            certainty=Certainty.HEURISTIC,
            raw_score=cv,
            evidence={
                "usable_intervals": usable_count,
                "total_intervals_observed": len(raw_intervals),
                "mean_interval_sec": mean_interval,
                "stdev_interval_sec": stdev_interval,
                "cv": cv,
                "max_cv_threshold": self._max_cv,
                "comparison": f"cv={cv:.4f} {'<=' if fired else '>'} max_cv={self._max_cv:.4f}",
            },
        )
