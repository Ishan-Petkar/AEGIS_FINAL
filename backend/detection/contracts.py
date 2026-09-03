"""
backend/detection/contracts.py — the detector-independent contracts the
Hybrid IDS is built on.

Two types carry everything: `FlowFeatures` (what a detector is allowed to
look at) and `DetectorVerdict` (what a detector is allowed to conclude).
`FusedDecision` is what the fusion engine produces from a set of verdicts.

Why `FlowFeatures` exists rather than passing `ReplayFlow` around
--------------------------------------------------------------------
`ReplayFlow` (`backend/replay_reader.py`) carries `label` and `is_attack`
— the CIC-IDS2017 GROUND TRUTH. Handing that object to a detector makes
label leakage a one-character mistake (`flow.is_attack` reads exactly like
a feature), and this project has already been bitten by that class of bug
twice: the circular-labeling bug recorded in PLAN_MASTER.md, and the
train/eval scaler leak fixed in the Phase C pass. `FlowFeatures` is a
projection that simply **does not contain** the ground-truth fields, so a
detector cannot read them even by accident, and a reviewer can verify that
by reading this dataclass instead of auditing every detector.

`is_honeytoken_use` IS included, and that is not an inconsistency: a
honeytoken touch is AEGIS's own deception instrumentation firing (see
`ReplayFlow.is_honeytoken_use`'s comment), not the dataset's opinion about
whether a row is an attack. It is a real observable signal, available at
detection time in production, which is exactly the test for whether
something belongs in this projection.

Also excluded: `source_row_id`, `source_dataset`, `timing_provenance`.
Those are provenance for persistence and honesty accounting, not signal —
a detector keying off `source_dataset` would be learning which file a row
came from.

Why the existing two detectors are adapted rather than rewritten
-----------------------------------------------------------------
`StreamingScorer.score_batch()` returns `ScoredFlow`, which carries
`z_scores` aligned to its own `feature_names`; `SupervisedFlowScorer`
returns `SupervisedScoredFlow`. Both are correct, both are load-bearing
for `explain()` and for `event_scores` rows, and both are pinned by large
existing test files. Rewriting them to emit `DetectorVerdict` natively
would be a behaviour change to two working, measured detectors in service
of tidiness. Instead `verdict_from_scored_flow()` /
`verdict_from_supervised()` below adapt them at the boundary, so the
hybrid layer sees a uniform contract and neither detector is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

# ---------------------------------------------------------------------------
# Detector names — the single source of truth for `event_scores.detector`
# ---------------------------------------------------------------------------
# `backend/ingest.py` already defines DETECTOR_VOLUMETRIC / DETECTOR_TRIPWIRE
# / DETECTOR_SUPERVISED as module constants and writes them into
# `EventScore.detector`. The new channels' names live here (this package is
# where the new detectors live) and are re-exported by ingest rather than
# redefined there, so one string per detector exists in the codebase.

#: Rule/signature engine (`backend/detection/signature.py`).
DETECTOR_SIGNATURE = "signature"

#: Temporal/periodicity detector (`backend/detection/beaconing.py`).
DETECTOR_BEACONING = "beaconing"

#: The fused hybrid decision itself. Persisted as its own `event_scores`
#: row so the combined verdict is auditable next to the per-detector rows
#: that produced it, rather than being recomputable-only.
DETECTOR_HYBRID = "hybrid"


# ---------------------------------------------------------------------------
# Certainty — preserves the tripwire/anomaly distinction structurally
# ---------------------------------------------------------------------------


class Certainty(str, Enum):
    """How a verdict was arrived at, which governs whether fusion may
    dilute it.

    The project's central detection finding (docs/DETECTION_STUDY.md) is
    that its three channels are not peers: the honeytoken tripwire has
    zero false positives BY CONSTRUCTION (a credential with no legitimate
    use cannot be legitimately used), while the volumetric channel
    measures ~0.02 precision. Averaging those two would be the single
    worst thing this fusion layer could do — it would let ~800 junk
    volumetric signals per replay day drag a confirmed compromise toward
    the middle, and P5-15 exists precisely to stop that.

    So certainty is part of the contract, not a weight:

      `CONFIRMED`  the signal cannot be a false positive on its own terms.
                   Fusion must escalate, never average (see
                   `HybridFusionEngine`'s precedence rule).
      `HEURISTIC`  a statistical or rule-based inference that can be
                   wrong. Fusion combines these probabilistically.
    """

    CONFIRMED = "confirmed"
    HEURISTIC = "heuristic"


class ThreatBand(str, Enum):
    """Coarse tier for a fused threat score.

    Bands, not a bare float, because the consumers are an operator's eye
    and an alert policy — both of which need "how bad, roughly" and
    neither of which benefits from three decimal places. Thresholds are
    configured (`BACKEND_SETTINGS.hybrid_band_*`), never literals here.
    """

    BENIGN = "benign"
    SUSPICIOUS = "suspicious"
    LIKELY = "likely"
    CONFIRMED = "confirmed"


class ResponseAction(str, Enum):
    """What the decision recommends. **Advisory only in this release.**

    `OBSERVE`/`ALERT` are what `backend/ingest.py` acts on today. `THROTTLE`
    and `BLOCK` are declared but never produced by `HybridFusionEngine` in
    this release, and nothing in the codebase consumes them — they exist so
    the future IPS policy layer extends an existing enum instead of forcing
    a breaking change to `FusedDecision`. See the "Deferred for IPS" note
    in `fusion.py`.
    """

    OBSERVE = "observe"
    ALERT = "alert"
    THROTTLE = "throttle"  # reserved for IPS; never emitted here
    BLOCK = "block"        # reserved for IPS; never emitted here


# ---------------------------------------------------------------------------
# FlowFeatures — the common representation
# ---------------------------------------------------------------------------

_EMPTY_EVIDENCE: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class FlowFeatures:
    """One flow, as a detector is allowed to see it.

    Frozen for the same reason `ReplayFlow` is: these objects are shared
    across detectors within a batch, and a detector mutating a shared flow
    would be a cross-detector side channel.

    Field set is deliberately a subset of `ReplayFlow` — see the module
    docstring on why `label`/`is_attack` are absent. Construct via
    `from_replay_flow()` rather than by hand outside tests, so the
    projection stays defined in exactly one place.
    """

    ts: datetime
    source_ip: str
    source_port: int
    destination_ip: str
    destination_port: int
    protocol: str
    duration_sec: float
    packets: int
    bytes: int
    bwd_packet_length_mean: float = 0.0
    init_win_bytes_forward: int = 0
    init_win_bytes_backward: int = 0
    average_packet_size: float = 0.0
    is_honeytoken_use: bool = False

    @classmethod
    def from_replay_flow(cls, flow: Any) -> "FlowFeatures":
        """Project a `ReplayFlow` (or any object with the same attribute
        names) into the detector-visible view.

        Duck-typed rather than importing `ReplayFlow` so this package has
        no dependency on the replay reader — `backend.inject` already
        proves flows can come from more than one producer, and tests
        construct lightweight stand-ins. Optional fields fall back to the
        same defaults `ReplayFlow` declares, so a pre-Ticket-5 fixture
        without them projects cleanly instead of raising.
        """
        return cls(
            ts=flow.ts,
            source_ip=flow.source_ip,
            source_port=flow.source_port,
            destination_ip=flow.destination_ip,
            destination_port=flow.destination_port,
            protocol=flow.protocol,
            duration_sec=flow.duration_sec,
            packets=flow.packets,
            bytes=flow.bytes,
            bwd_packet_length_mean=getattr(flow, "bwd_packet_length_mean", 0.0),
            init_win_bytes_forward=getattr(flow, "init_win_bytes_forward", 0),
            init_win_bytes_backward=getattr(flow, "init_win_bytes_backward", 0),
            average_packet_size=getattr(flow, "average_packet_size", 0.0),
            is_honeytoken_use=getattr(flow, "is_honeytoken_use", False),
        )

    @property
    def pair_key(self) -> tuple[str, str]:
        """`(source_ip, destination_ip)` — the aggregation key for any
        detector that needs cross-flow history (currently the beaconing
        detector). Ports are excluded on purpose: a beacon that rotates
        source ports is still the same channel, and keying on the 5-tuple
        would split its history into singletons and hide exactly the
        pattern being looked for.
        """
        return (self.source_ip, self.destination_ip)


# ---------------------------------------------------------------------------
# DetectorVerdict — one detector's opinion about one flow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectorVerdict:
    """One detector's conclusion about one flow.

    Parameters
    ----------
    detector:
        Stable detector name. MUST be one of the `DETECTOR_*` constants
        (here or in `backend/ingest.py`) because it is written straight
        into `event_scores.detector`, which the Research Console and
        `GET /api/events` both group by.
    fired:
        Whether this detector considers the flow anomalous. Kept separate
        from `calibrated_score` because a detector can be confident the
        flow is fine (score 0.02, fired False) or can fire on a rule with
        no meaningful score at all.
    raw_score:
        The detector's native score in its own units — sklearn
        `decision_function` output, a rule's match count, whatever. Not
        comparable across detectors; persisted for auditability and shown
        in `explain()` payloads. `None` when the detector has no native
        scalar (a rule match).
    calibrated_score:
        The one cross-detector-comparable number: an approximate
        P(malicious) in [0, 1]. This is what fusion consumes. A detector
        with no principled calibration must say so in `evidence` rather
        than inventing precision.
    reliability:
        How much this detector's `calibrated_score` should be trusted,
        in [0, 1]. **Configured, not chosen by the detector** — it comes
        from `BACKEND_SETTINGS.hybrid_weight_*`, whose defaults are the
        MEASURED precisions in docs/DETECTION_STUDY.md (volumetric ~0.02,
        supervised ~0.998 in-distribution, tripwire 1.0). Passed on the
        verdict rather than looked up inside fusion so that an
        `event_scores` row records the weight actually used at decision
        time, not whatever the config says on a later read.
    certainty:
        See `Certainty`. Governs whether fusion may average this signal.
    evidence:
        Free-form provenance: WHY this verdict. The rule id that matched,
        the z-scores that were extreme, the inter-arrival statistics, the
        honest caveat that a score is uncalibrated. Must be
        JSON-serialisable — `backend/ingest.py` already runs everything it
        broadcasts through `_jsonable()`, and this lands in a WebSocket
        envelope. Empty is legal but discouraged: a verdict with no
        evidence is unauditable, and "show me why" is the question an
        operator actually asks.
    """

    detector: str
    fired: bool
    calibrated_score: float
    reliability: float
    certainty: Certainty = Certainty.HEURISTIC
    raw_score: Optional[float] = None
    evidence: Mapping[str, Any] = _EMPTY_EVIDENCE

    def __post_init__(self) -> None:
        # Fail loudly at construction rather than letting an out-of-range
        # score silently distort a noisy-OR product several layers later.
        if not 0.0 <= self.calibrated_score <= 1.0:
            raise ValueError(
                f"{self.detector}: calibrated_score must be in [0, 1], got "
                f"{self.calibrated_score!r}. It is an approximate "
                f"P(malicious) shared across detectors, not a native score "
                f"— put native units in raw_score instead."
            )
        if not 0.0 <= self.reliability <= 1.0:
            raise ValueError(
                f"{self.detector}: reliability must be in [0, 1], got "
                f"{self.reliability!r}."
            )


# ---------------------------------------------------------------------------
# FusedDecision — what the fusion engine produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FusedDecision:
    """The hybrid layer's combined conclusion about one flow.

    `threat_score` is the fused approximate P(malicious); `band` is its
    configured tier; `action` is advisory (see `ResponseAction`).
    `verdicts` keeps every contributing verdict so the decision is
    reconstructible from its inputs — an operator asking "why is this
    critical" gets the actual per-detector evidence, not a summary of it.
    `rationale` is a short human-readable sentence for the UI.
    """

    threat_score: float
    band: ThreatBand
    action: ResponseAction
    rationale: str
    verdicts: tuple[DetectorVerdict, ...] = field(default_factory=tuple)

    @property
    def fired_detectors(self) -> tuple[str, ...]:
        """Names of the detectors that actually fired, in verdict order.
        Convenience for evidence payloads and assertions."""
        return tuple(v.detector for v in self.verdicts if v.fired)

    @property
    def has_confirmed_signal(self) -> bool:
        """True when any firing verdict was `Certainty.CONFIRMED`.

        `backend/ingest.py`'s alert policy keys off this rather than off
        `band`, so the existing "tripwire always alerts, volumetric is
        suppressed" guarantee (P5-15) survives any future retuning of the
        band thresholds.
        """
        return any(v.fired and v.certainty is Certainty.CONFIRMED for v in self.verdicts)


# ---------------------------------------------------------------------------
# FlowDetector — the protocol new detectors implement
# ---------------------------------------------------------------------------


@runtime_checkable
class FlowDetector(Protocol):
    """A detector in the hybrid layer.

    Batch-oriented, matching the rest of the pipeline: P5-10 measured
    per-event processing at 1.78 ms/event against a 0.747 ms/event budget
    (2.4x over) versus ~0.019 ms/event batched, so a per-flow detector
    interface would reintroduce a performance problem the engine was
    restructured to avoid. It also lets a stateful detector
    (`BeaconingDetector`) update its history once per batch.

    Deliberately NOT `src/detectors/base.BaseDetector`. That protocol is
    `fit`/`predict`/`score_samples` over an `ndarray` with the sklearn
    -1/+1 convention, designed for the offline benchmark loop, and
    registering anything there makes `evaluation.run_evaluation()` fit it
    on a benign-only label-free split (contract: "registered =>
    benchmarked"). K6 records the HIGH-severity bug that caused when a
    supervised detector was registered there. These detectors are live,
    stateful and not all fittable, so they get their own contract and stay
    out of that registry.
    """

    #: Stable name, written to `event_scores.detector`.
    name: str

    def examine(self, flows: Sequence[FlowFeatures]) -> list[DetectorVerdict]:
        """Return exactly one verdict per input flow, in input order.

        Length and order are part of the contract — `backend/ingest.py`
        zips verdicts against `inserted_ids` positionally, so a detector
        that filters or reorders would silently attribute verdicts to the
        wrong events.
        """
        ...


# ---------------------------------------------------------------------------
# Adapters for the two existing detectors
# ---------------------------------------------------------------------------


def verdict_from_scored_flow(
    scored: Any, reliability: float, detector: str
) -> DetectorVerdict:
    """Adapt a `backend.streaming.ScoredFlow` into a `DetectorVerdict`.

    `ScoredFlow.calibrated_score` is already a sigmoid-calibrated [0, 1]
    figure, so it maps straight across. The z-scores come along as
    evidence because they are the volumetric channel's actual "why" and
    `explain()` already treats them that way.

    Always `Certainty.HEURISTIC`: this is the channel measured at ~0.02
    precision, and the honest consequence of that measurement is that it
    may never escalate anything on its own.
    """
    return DetectorVerdict(
        detector=detector,
        fired=bool(scored.is_anomaly),
        calibrated_score=float(scored.calibrated_score),
        reliability=reliability,
        certainty=Certainty.HEURISTIC,
        raw_score=float(scored.raw_score),
        evidence={
            "z_scores": [float(z) for z in scored.z_scores],
            "channel": "volumetric",
        },
    )


def verdict_from_supervised(
    scored: Any, reliability: float, detector: str
) -> DetectorVerdict:
    """Adapt a `backend.supervised_detector.SupervisedScoredFlow`.

    `calibrated_score` here is a native `P(attack)` from the forest's
    `predict_proba`, which is why it needs no rescaling.

    `Certainty.HEURISTIC` despite the channel's 0.998 measured precision,
    and that is the deliberate call: the SAME study measured **precision
    0.000** on a novel attack family (train Tue+Wed, test Friday Bot). A
    detector that is excellent on what it has seen and blind to everything
    else cannot be allowed to confirm anything by itself — its evidence
    carries the caveat so the payload states it too.
    """
    return DetectorVerdict(
        detector=detector,
        fired=bool(scored.is_anomaly),
        calibrated_score=float(scored.calibrated_score),
        reliability=reliability,
        certainty=Certainty.HEURISTIC,
        raw_score=float(scored.raw_score),
        evidence={
            "channel": "supervised",
            "caveat": (
                "known-threat channel: measured precision 0.000 on an "
                "attack family absent from training (docs/DETECTION_STUDY.md "
                "Test 2)"
            ),
        },
    )


def verdict_from_tripwire(
    fired: bool, tripwire_score: float, reliability: float, detector: str
) -> DetectorVerdict:
    """Build the tripwire's verdict — the only `CONFIRMED` producer.

    A honeytoken is a credential with zero legitimate use, so a touch is
    not evidence of compromise, it IS compromise; `calibrated_score` is
    therefore 1.0 when fired and there is no threshold to tune. This is
    the signal P5-15 guarantees always alerts, and `Certainty.CONFIRMED`
    is how that guarantee survives contact with the fusion engine.
    """
    return DetectorVerdict(
        detector=detector,
        fired=bool(fired),
        calibrated_score=1.0 if fired else 0.0,
        reliability=reliability,
        certainty=Certainty.CONFIRMED if fired else Certainty.HEURISTIC,
        raw_score=float(tripwire_score) if fired else None,
        evidence={
            "channel": "deception",
            "signal": "honeytoken_credential_used" if fired else "no_honeytoken_touch",
            "false_positive_rate": "zero by construction" if fired else None,
        },
    )
