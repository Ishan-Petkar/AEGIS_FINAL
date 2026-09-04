"""
backend/ips/policy.py — the IPS Policy / Decision Engine.

Pure and stateless, mirroring `backend/detection/fusion.py`'s
`HybridFusionEngine`: `IPSPolicyEngine.decide()` is a function of its
arguments only (no I/O, no mutable state, no clock), so it needs no
special test doubles — construct one, call `.decide()`, assert on the
returned `PreventionDecision`. ALL state (active mitigations, TTL, audit
history, de-duplication) lives one layer up, in `IngestPipeline`
(mirroring exactly where CII debounce and alert debounce state already
live) — this class only answers "given what the Hybrid IDS layer, the
CII engine, and the asset registry already know about this flow, right
now, in isolation, what should the IPS layer do about it" — never "have
we already acted on this asset recently" or "is there already an active
mitigation to escalate or skip".

Consumes ONLY `backend.detection.contracts.FusedDecision` — already-
fused, already-weighted evidence from all five Hybrid IDS channels —
plus two numbers the caller already has to hand (asset criticality, CII
median impact). It does not run its own attack detection, examine raw
flows, or import anything from `backend.detection.beaconing` /
`.signature` — per the requirement, "Consume Hybrid IDS outputs ...
rather than implementing independent attack detection." It also never
imports or modifies `backend.detection.fusion` / `.contracts` beyond
reading `FusedDecision`/`ThreatBand` — see `backend/ips/contracts.py`'s
module docstring for why `ResponseAction.THROTTLE`/`.BLOCK` stay
untouched rather than being extended for this layer.
"""

from __future__ import annotations

from typing import Optional

from backend.config import BACKEND_SETTINGS
from backend.detection.contracts import FusedDecision, ThreatBand
from backend.ips.contracts import PreventionAction, PreventionDecision


class IPSPolicyEngine:
    """Maps `(FusedDecision, asset criticality, CII median impact)` to a
    `PreventionDecision`.

    Optional-override constructor (CLAUDE.md section 5): every threshold
    falls back to `BACKEND_SETTINGS` inside `__init__`, not inside
    `decide()`, so a constructed engine's behaviour is fixed at
    construction time — the same pattern `HybridFusionEngine` uses for
    its own band thresholds.
    """

    def __init__(
        self,
        min_corroborating_detectors: Optional[int] = None,
        rate_limit_min_threat_score: Optional[float] = None,
        block_min_threat_score: Optional[float] = None,
        block_min_asset_criticality: Optional[float] = None,
        quarantine_min_asset_criticality: Optional[float] = None,
        quarantine_min_cii_median: Optional[float] = None,
        rate_limit_ttl_sec: Optional[float] = None,
        block_ttl_sec: Optional[float] = None,
        quarantine_ttl_sec: Optional[float] = None,
    ) -> None:
        settings = BACKEND_SETTINGS
        self._min_corroborating_detectors = (
            settings.ips_min_corroborating_detectors
            if min_corroborating_detectors is None
            else min_corroborating_detectors
        )
        self._rate_limit_min_threat_score = (
            settings.ips_rate_limit_min_threat_score
            if rate_limit_min_threat_score is None
            else rate_limit_min_threat_score
        )
        self._block_min_threat_score = (
            settings.ips_block_min_threat_score
            if block_min_threat_score is None
            else block_min_threat_score
        )
        self._block_min_asset_criticality = (
            settings.ips_block_min_asset_criticality
            if block_min_asset_criticality is None
            else block_min_asset_criticality
        )
        self._quarantine_min_asset_criticality = (
            settings.ips_quarantine_min_asset_criticality
            if quarantine_min_asset_criticality is None
            else quarantine_min_asset_criticality
        )
        self._quarantine_min_cii_median = (
            settings.ips_quarantine_min_cii_median
            if quarantine_min_cii_median is None
            else quarantine_min_cii_median
        )
        self._rate_limit_ttl_sec = (
            settings.ips_rate_limit_ttl_sec
            if rate_limit_ttl_sec is None
            else rate_limit_ttl_sec
        )
        self._block_ttl_sec = (
            settings.ips_block_ttl_sec if block_ttl_sec is None else block_ttl_sec
        )
        self._quarantine_ttl_sec = (
            settings.ips_quarantine_ttl_sec
            if quarantine_ttl_sec is None
            else quarantine_ttl_sec
        )

    def decide(
        self,
        fused: FusedDecision,
        target_asset: str,
        asset_criticality: float,
        cii_median: Optional[float] = None,
    ) -> PreventionDecision:
        """The core policy. See the module docstring for scope.

        Corroboration requirement — the requirement's "never block every
        anomaly automatically," made concrete: active prevention
        (RATE_LIMIT/BLOCK/QUARANTINE) requires EITHER a confirmed signal
        (`fused.has_confirmed_signal` — today only the honeytoken
        tripwire, which cannot false-positive by construction) OR at
        least `min_corroborating_detectors` independently fired
        detectors. A single heuristic detector firing alone — even at a
        high individual score — can only ever reach ALERT, never active
        prevention. This guard cannot be tuned away by relaxing
        threat_score thresholds alone, because a threat_score threshold
        and a detector-count threshold defend against two different
        failure modes: one miscalibrated detector screaming loudly, vs.
        genuinely independent corroborating evidence.

        Tier selection, once corroborated, is strongest-qualifying-tier:
        QUARANTINE is the most restrictively gated of the three (needs
        BOTH high asset criticality AND a real projected blast radius,
        not criticality alone — an asset can be intrinsically critical
        yet, right now, have nothing meaningful left to protect
        downstream, in which case isolating it gains nothing and only
        costs an operator their own visibility into it). BLOCK needs
        high threat_score and sufficient (but lower) criticality.
        RATE_LIMIT is the floor for any corroborated signal that clears
        its own threshold but neither of the above.
        """
        n_fired = len(fused.fired_detectors)
        corroborated = (
            fused.has_confirmed_signal or n_fired >= self._min_corroborating_detectors
        )
        evidence = self._evidence(fused, asset_criticality, cii_median, n_fired, corroborated)

        if fused.band in (ThreatBand.BENIGN, ThreatBand.SUSPICIOUS):
            return PreventionDecision(
                action=PreventionAction.OBSERVE,
                target_asset=target_asset,
                confidence=fused.threat_score,
                reason=f"band {fused.band.value} below the alert threshold; no prevention warranted",
                evidence=evidence,
            )

        if not corroborated:
            return PreventionDecision(
                action=PreventionAction.ALERT,
                target_asset=target_asset,
                confidence=fused.threat_score,
                reason=(
                    f"band {fused.band.value} clears the alert threshold, but only "
                    f"{n_fired} detector(s) fired (need "
                    f"{self._min_corroborating_detectors} or a confirmed signal) — "
                    "prevention withheld pending corroboration"
                ),
                evidence=evidence,
            )

        if (
            fused.threat_score >= self._block_min_threat_score
            and asset_criticality >= self._quarantine_min_asset_criticality
            and cii_median is not None
            and cii_median >= self._quarantine_min_cii_median
        ):
            return PreventionDecision(
                action=PreventionAction.QUARANTINE,
                target_asset=target_asset,
                confidence=fused.threat_score,
                reason=(
                    f"corroborated signal (band {fused.band.value}) against a critical "
                    f"asset (criticality {asset_criticality:.2f}) with a real projected "
                    f"blast radius (CII median {cii_median:.3f}) — isolate"
                ),
                evidence=evidence,
                ttl_sec=self._quarantine_ttl_sec,
            )

        if (
            fused.threat_score >= self._block_min_threat_score
            and asset_criticality >= self._block_min_asset_criticality
        ):
            return PreventionDecision(
                action=PreventionAction.BLOCK,
                target_asset=target_asset,
                confidence=fused.threat_score,
                reason=(
                    f"corroborated signal (band {fused.band.value}, threat_score "
                    f"{fused.threat_score:.3f}) against an asset of sufficient "
                    f"criticality ({asset_criticality:.2f}) — block"
                ),
                evidence=evidence,
                ttl_sec=self._block_ttl_sec,
            )

        if fused.threat_score >= self._rate_limit_min_threat_score:
            return PreventionDecision(
                action=PreventionAction.RATE_LIMIT,
                target_asset=target_asset,
                confidence=fused.threat_score,
                reason=(
                    f"corroborated signal (band {fused.band.value}, {n_fired} detectors "
                    "fired) below the block threshold or target asset criticality — "
                    "rate-limit pending further evidence"
                ),
                evidence=evidence,
                ttl_sec=self._rate_limit_ttl_sec,
            )

        return PreventionDecision(
            action=PreventionAction.ALERT,
            target_asset=target_asset,
            confidence=fused.threat_score,
            reason=(
                f"corroborated (band {fused.band.value}) but threat_score "
                f"{fused.threat_score:.3f} below every active-prevention threshold"
            ),
            evidence=evidence,
        )

    @staticmethod
    def _evidence(
        fused: FusedDecision,
        asset_criticality: float,
        cii_median: Optional[float],
        n_fired: int,
        corroborated: bool,
    ) -> dict:
        """A JSON-safe evidence snapshot, persisted verbatim into
        `IpsAction.evidence` and included in the `ips_action` broadcast
        envelope — the requirement's "what, why, evidence" audit fields."""
        return {
            "threat_score": fused.threat_score,
            "band": fused.band.value,
            "fired_detectors": list(fused.fired_detectors),
            "n_fired": n_fired,
            "has_confirmed_signal": fused.has_confirmed_signal,
            "corroborated": corroborated,
            "asset_criticality": asset_criticality,
            "cii_median": cii_median,
            "fusion_rationale": fused.rationale,
        }
