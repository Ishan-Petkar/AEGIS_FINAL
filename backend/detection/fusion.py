"""
backend/detection/fusion.py — the Hybrid IDS combinator.

`HybridFusionEngine` takes the per-detector opinions defined in
`backend/detection/contracts.py` (`DetectorVerdict`, one per detector per
flow) and produces one `FusedDecision` per flow. It is the layer that
turns "five channels each said something" into "here is one threat
score, one band, one recommended action, and the reasoning behind it."

Relationship to `backend.streaming.fuse_tripwire_confidence`
--------------------------------------------------------------
`fuse_tripwire_confidence` (`backend/streaming.py`) already exists and is
NOT touched or replaced here. It is a narrower, older thing: a 2-channel
OR-fusion between exactly the volumetric detector and the tripwire,
pinned bit-for-bit to `src/core/pipeline.py::run_analysis()` by
`tests/test_streaming_scorer.py::test_fusion_matches_pipeline`, and it
still runs, unchanged, as part of `backend/ingest.py`'s scoring path —
its `is_anomaly` / `confidence` outputs continue to drive the alert
policy that shipped before this module existed
(`BACKEND_SETTINGS.hybrid_gates_alerts` defaults to `False` specifically
so that policy keeps authority; see that field's docstring).

`HybridFusionEngine` is an ADDITIONAL layer, not a superset or a
successor. It combines up to five channels (volumetric, supervised,
tripwire, signature, beaconing) via `DetectorVerdict`, a richer contract
that carries a calibrated score, a configured reliability weight, and a
`Certainty` tag `fuse_tripwire_confidence`'s bare boolean arrays have no
room for. The two coexist deliberately: this module's `FusedDecision` is
persisted as its own `event_scores` row (`DETECTOR_HYBRID`) alongside,
not instead of, the existing volumetric/tripwire rows, so the new
combined view is auditable without disturbing a measured, pinned
pipeline.

Why noisy-OR, not majority vote or averaging
-----------------------------------------------
For the heuristic channels (Step 2 below), this engine combines
`calibrated_score x reliability` values with the noisy-OR rule:

    threat_score = 1 - Π(1 - p_i)   over verdicts that fired

Noisy-OR is the standard combinator for independent evidence bearing on
one hypothesis ("is this flow malicious"), and it has three properties
none of the obvious alternatives share:

  * Monotonic — adding another firing detector can only raise the score
    (each `(1 - p_i)` factor is in `[0, 1]`, so the product only shrinks
    and `1 - product` only grows). More evidence never quietly lowers a
    decision, which a human reading a dashboard implicitly assumes.
  * Bounded in `[0, 1]` by construction, with no separate clamp needed —
    unlike a raw sum of scores, which needs an ad hoc cap once enough
    channels fire (see `cii_calculator.py`'s note on `cii_max_value`
    for what that degeneracy looks like when it is not caught).
  * Honestly weight-sensitive — a detector's contribution is capped by
    its own reliability. The volumetric channel's calibrated score can
    be a shouting 1.0, but at `reliability=0.02` it can never contribute
    more than 0.02 to the product term, so it stays nearly inert no
    matter how confident it claims to be. That is the whole point of
    carrying a MEASURED precision as `reliability` (see
    `BACKEND_SETTINGS.hybrid_weight_volumetric`'s docstring) instead of
    a hand-tuned "importance" knob.

  Contrast with the alternatives this module deliberately does NOT use:

  * Majority vote — throws away both confidence and weight. A detector
    firing at calibrated_score 0.99 and one firing at 0.51 count as the
    same one vote, and a barely-reliable detector's vote counts exactly
    as much as a proven one's.
  * Averaging — lets a quiet, reliable detector cancel a loud one, and
    (much worse, see Step 1) is exactly the operation that must never
    touch a `Certainty.CONFIRMED` signal. `Certainty`'s docstring in
    `contracts.py` spells out why: averaging a honeytoken touch against
    ~800 junk volumetric signals a day would drag a confirmed compromise
    toward the middle. Noisy-OR sidesteps this for the heuristic case
    too (a weak second opinion can only nudge the score up, never down),
    but Step 1's precedence rule is what makes the CONFIRMED guarantee
    absolute rather than merely "usually fine."

Non-firing verdicts contribute nothing
-----------------------------------------
A verdict with `fired=False` is dropped from the product entirely — it
is treated as *absence of evidence*, not as *evidence of absence*. A
detector that stayed quiet did not affirmatively vouch for the flow
being benign (most of these detectors have no calibrated notion of
"confidently benign"; they simply did not cross their trigger
condition), so folding a `(1 - p_i)` term for a non-firing detector into
the product would silently manufacture confidence in "benign" the
detector never asserted. This is also why `fuse_batch` on an all-quiet
batch returns `threat_score=0.0` via an empty product (⇒ `1 - 1 = 0`)
rather than some detector "voting" for benign explicitly.

Deferred for IPS (this release is advisory-only)
----------------------------------------------------
`ResponseAction.THROTTLE` / `.BLOCK` exist in the enum but are never
produced here — see that enum's docstring. `HybridFusionEngine` only
ever emits `OBSERVE` or `ALERT`. A future intrusion-prevention policy
layer would plug in above this module's `FusedDecision` (band +
threat_score + verdicts are all it would need) to decide, e.g., "band
CONFIRMED against a Purdue-level-0 asset -> BLOCK", "band LIKELY from a
single novel-signature match -> THROTTLE pending signature review". That
policy is out of scope here on purpose: it needs asset criticality,
current network load, and a rollback story this module has no access to
and no business modeling.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from backend.config import BACKEND_SETTINGS
from backend.detection.contracts import (
    Certainty,
    DetectorVerdict,
    FusedDecision,
    ResponseAction,
    ThreatBand,
)

# ---------------------------------------------------------------------------
# HybridFusionEngine
# ---------------------------------------------------------------------------


class HybridFusionEngine:
    """Combines `DetectorVerdict`s into one `FusedDecision` per flow.

    Pure and stateless: no I/O, no database access, nothing cached
    between calls. `fuse()` is a function of its argument alone, which is
    what lets `fuse_batch()` just be a loop and what makes this class
    trivial to unit test without any pipeline scaffolding.

    Parameters
    ----------
    band_suspicious, band_likely, band_confirmed:
        Optional-override thresholds (see module-level convention used
        throughout this codebase: a public entrypoint takes
        `param: T | None = None` and falls back to `BACKEND_SETTINGS`
        inside the body, so tests and sweeps can override without
        mutating the frozen settings singleton). Defaults are
        `BACKEND_SETTINGS.hybrid_band_suspicious` /
        `hybrid_band_likely` / `hybrid_band_confirmed`, which
        `BackendSettings` itself validates as strictly increasing
        (`_check_hybrid_bands_ordered`), so this constructor does not
        re-validate an override's ordering — passing an unordered
        override is a caller bug, same as it would be for the settings
        object itself.
    """

    def __init__(
        self,
        band_suspicious: Optional[float] = None,
        band_likely: Optional[float] = None,
        band_confirmed: Optional[float] = None,
    ) -> None:
        self._band_suspicious = (
            band_suspicious
            if band_suspicious is not None
            else BACKEND_SETTINGS.hybrid_band_suspicious
        )
        self._band_likely = (
            band_likely if band_likely is not None else BACKEND_SETTINGS.hybrid_band_likely
        )
        self._band_confirmed = (
            band_confirmed
            if band_confirmed is not None
            else BACKEND_SETTINGS.hybrid_band_confirmed
        )

    @property
    def band_thresholds(self) -> Mapping[str, float]:
        """Read-only view of the configured band thresholds, for
        `/api/stats`-style introspection endpoints that want to show an
        operator what the current bands mean without reaching into
        `BACKEND_SETTINGS` (which may have been overridden on this
        instance) or hardcoding the threshold names a second time."""
        return {
            "suspicious": self._band_suspicious,
            "likely": self._band_likely,
            "confirmed": self._band_confirmed,
        }

    # -- public API ----------------------------------------------------

    def fuse(self, verdicts: Sequence[DetectorVerdict]) -> FusedDecision:
        """Fuse every detector's opinion about ONE flow into one decision.

        See the module docstring for the full rationale. Short version:
        a fired `Certainty.CONFIRMED` verdict wins outright (Step 1); ✓
        otherwise every fired heuristic verdict's `calibrated_score x
        reliability` combines via noisy-OR (Step 2); the result is banded
        against the configured thresholds (Step 3) and turned into an
        advisory `OBSERVE`/`ALERT` action (Step 4).
        """
        verdicts = tuple(verdicts)

        if not verdicts:
            return FusedDecision(
                threat_score=0.0,
                band=ThreatBand.BENIGN,
                action=ResponseAction.OBSERVE,
                rationale="no detectors reported",
                verdicts=verdicts,
            )

        # ---- 72-dash rules ----
        # Step 1 — confirmed-signal precedence. NOT averaging: a fired
        # CONFIRMED verdict (today, only the honeytoken tripwire) decides
        # the outcome by itself, immune to dilution by any number of weak
        # or even weak-but-fired heuristic channels alongside it. This is
        # P5-15's guarantee, restated at the fusion boundary.
        confirmed = [
            v for v in verdicts if v.fired and v.certainty is Certainty.CONFIRMED
        ]
        if confirmed:
            names = ", ".join(v.detector for v in confirmed)
            return FusedDecision(
                threat_score=1.0,
                band=ThreatBand.CONFIRMED,
                action=ResponseAction.ALERT,
                rationale=f"{_confirmed_rationale(confirmed)} (confirmed via {names})"
                if len(confirmed) > 1
                else _confirmed_rationale(confirmed),
                verdicts=verdicts,
            )

        # ---- 72-dash rules ----
        # Step 2 — weighted noisy-OR over fired heuristic verdicts. Verdicts
        # that did not fire contribute nothing: absence of evidence is not
        # evidence of benignity (see module docstring). `p_i` is each
        # firing detector's calibrated P(malicious) discounted by how much
        # that detector's score should be trusted.
        fired_heuristic = [v for v in verdicts if v.fired]

        if not fired_heuristic:
            return FusedDecision(
                threat_score=0.0,
                band=ThreatBand.BENIGN,
                action=ResponseAction.OBSERVE,
                rationale=f"{len(verdicts)} detector(s) ran, none fired",
                verdicts=verdicts,
            )

        product_survival = 1.0
        for v in fired_heuristic:
            p_i = v.calibrated_score * v.reliability
            product_survival *= 1.0 - p_i
        threat_score = 1.0 - product_survival

        # ---- 72-dash rules ----
        # Step 3 — band from thresholds. `>=` semantics throughout, so a
        # score landing exactly on a boundary gets the higher band.
        if threat_score >= self._band_confirmed:
            band = ThreatBand.CONFIRMED
        elif threat_score >= self._band_likely:
            band = ThreatBand.LIKELY
        elif threat_score >= self._band_suspicious:
            band = ThreatBand.SUSPICIOUS
        else:
            band = ThreatBand.BENIGN

        # ---- 72-dash rules ----
        # Step 4 — action. ALERT at LIKELY or above, OBSERVE otherwise.
        # Never THROTTLE/BLOCK in this release — see the module docstring's
        # "Deferred for IPS" note.
        action = (
            ResponseAction.ALERT
            if band in (ThreatBand.LIKELY, ThreatBand.CONFIRMED)
            else ResponseAction.OBSERVE
        )

        rationale = _heuristic_rationale(fired_heuristic, threat_score)

        return FusedDecision(
            threat_score=threat_score,
            band=band,
            action=action,
            rationale=rationale,
            verdicts=verdicts,
        )

    def fuse_batch(
        self, verdicts_per_flow: Sequence[Sequence[DetectorVerdict]]
    ) -> list[FusedDecision]:
        """`fuse()` applied to each flow's verdicts, in order.

        Batch-shaped to match the rest of the pipeline (see
        `FlowDetector`'s docstring in `contracts.py` for why per-flow
        interfaces were rejected on measured performance grounds); this
        engine itself does no batching optimisation because noisy-OR over
        a handful of verdicts is not where the pipeline's time goes — it
        is provided so callers do not have to write the loop themselves
        and so a future optimisation has one call site to change.
        """
        return [self.fuse(verdicts) for verdicts in verdicts_per_flow]


# ---------------------------------------------------------------------------
# Rationale helpers — short human-readable sentences for the UI
# ---------------------------------------------------------------------------


def _confirmed_rationale(confirmed: Sequence[DetectorVerdict]) -> str:
    """Terse sentence for the Step-1 precedence path.

    Special-cases the tripwire's own evidence shape (`signal:
    "honeytoken_credential_used"`) into the exact phrasing product asked
    for; falls back to a generic "confirmed" statement for any future
    CONFIRMED-certainty detector that isn't the tripwire, so this does
    not silently mis-describe a new channel.
    """
    first = confirmed[0]
    signal = first.evidence.get("signal") if first.evidence else None
    if signal == "honeytoken_credential_used":
        return "honeytoken credential used (confirmed)"
    return f"{first.detector} confirmed"


def _heuristic_rationale(fired: Sequence[DetectorVerdict], threat_score: float) -> str:
    """Terse sentence for the Step-2 noisy-OR path, e.g.
    ``"supervised 0.91 x0.90, beaconing 0.60 x0.50 -> 0.86"``.

    Full per-detector detail (raw scores, evidence dicts) lives in
    `FusedDecision.verdicts`; this string is only the UI-facing summary,
    so it is kept to calibrated-score x reliability pairs plus the final
    fused number.
    """
    parts = [f"{v.detector} {v.calibrated_score:.2f} x{v.reliability:.2f}" for v in fired]
    return f"{', '.join(parts)} -> {threat_score:.2f}"
