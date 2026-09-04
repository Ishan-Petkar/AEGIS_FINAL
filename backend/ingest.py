"""
ingest.py — Phase 5, Ticket #7: score -> persist -> broadcast, CII debounce.

The consumer end of the replay pipeline. `ReplayEngine` (Ticket #6) emits
micro-batches of `ReplayFlow`; `IngestPipeline` is the callable that
receives them, and its `__call__(batch, meta)` signature is exactly
`replay_engine.Consumer`, so it drops straight in::

    engine = ReplayEngine(consumer=IngestPipeline(scorer=scorer))

Per batch, in order:

  1. score      -- ONE StreamingScorer.score_batch() call (never per-event)
  2. tripwire   -- the real TripwireDetector, not a reimplementation
  3. fuse       -- streaming.fuse_tripwire_confidence (OR + escalation)
  4. resolve    -- AssetRegistry.resolve() on source/destination IPs
  5. persist    -- events + event_scores, one transaction, batch order kept
  6. CII        -- compute_cascading_impact_full, debounced per origin asset
  7. alert      -- policy-gated (see "Alert policy" below), + explanation
  8. broadcast  -- typed envelopes, AFTER commit

Invariants honoured
-------------------
Invariant A -- nothing under `src/` is modified; this module only imports
    from it.
Invariant B -- no model refit in the streaming path. This module never
    calls `fit`/`fit_transform`; it calls `StreamingScorer.score_batch()`,
    which is itself pinned by two tests in tests/test_streaming_scorer.py.
Invariant C -- tripwire detection and confidence fusion are NOT
    reimplemented. `TripwireDetector` (src/deception/tripwire.py) and
    `streaming.fuse_tripwire_confidence` (Ticket #5) are imported and used
    as-is.
Invariant D -- one graph authority. The criticality map handed to the CII
    engine is derived from `backend.seed.compute_seed_rows()`, which is
    itself built from `graph_manager.build_graph()`. No second topology.

Alert policy (the load-bearing design decision)
-----------------------------------------------
"Anomaly detected" is deliberately NOT the same as "raise an alert".

`docs/DETECTION_STUDY.md` measured the unsupervised volumetric detector on
real replayed friday-morning traffic at **5 true positives against 811
false positives** (precision ~0.02). Raising an operator alert per
volumetric anomaly therefore produces ~800 junk rows per replay day, and
buries the one tripwire alert the demo's headline moment depends on.

So the channels are treated according to their measured worth:

  * tripwire fired  -> ALWAYS alert, severity "critical". A honeytoken has
    zero legitimate use anywhere in the system, so it cannot produce a
    false positive by construction (DETECTION_STUDY section 5).
  * volumetric only -> suppressed by default
    (`BACKEND_SETTINGS.alert_on_volumetric`), and even when enabled must
    clear `alert_volumetric_min_calibrated_score`.

Suppression is *alerting only*. Volumetric anomalies are still scored,
still written to `event_scores`, and still broadcast on the event channel,
so they remain visible in the live feed and the counters. Nothing is
hidden -- it just does not page an operator.

Both channels are additionally de-duplicated per asset by
`alert_asset_debounce_sec`: a honeytoken touched 400 times in a burst is
one incident, not 400 alerts.

Failure semantics
-----------------
Persistence failures RAISE. `ReplayEngine._emit_batch` catches consumer
exceptions, logs them, and increments `consumer_error_count` /
`consumer_failed_flow_count` (Ticket #6) -- so raising is what makes those
counters honest. Swallowing a DB error here would report a healthy replay
that persisted nothing.

Broadcast failures do NOT raise. Broadcasting happens strictly after the
transaction commits, and a dead WebSocket must never roll back or re-drive
data that is already durably in Postgres.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol, Sequence

import numpy as np
import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.config import BACKEND_SETTINGS, BackendSettings
from backend.db import session_scope
from backend.detection.beaconing import BeaconingDetector
from backend.detection.contracts import (
    DETECTOR_BEACONING,
    DETECTOR_HYBRID,
    DETECTOR_SIGNATURE,
    DETECTOR_TGNN,
    DetectorVerdict,
    FlowFeatures,
    FusedDecision,
    ResponseAction,
    ThreatBand,
    verdict_from_scored_flow,
    verdict_from_supervised,
    verdict_from_tripwire,
)
from backend.detection.fusion import HybridFusionEngine
from backend.detection.signature import SignatureEngine
from backend.detection.tgnn import TGNNDetector
from backend.ips.contracts import (
    ACTIVE_PREVENTION_ACTIONS,
    PREVENTION_SEVERITY,
    ActionStatus,
    EnforcementAdapter,
    PreventionAction,
    PreventionDecision,
)
from backend.ips.enforcement import SimulatedEnforcementAdapter
from backend.ips.policy import IPSPolicyEngine
from backend.models import Alert, CiiSnapshot, Event, EventScore, IpsAction
from backend.replay_engine import BatchMeta
from backend.replay_reader import ReplayFlow
from backend.retention import prune_events
from backend.seed import compute_seed_rows
from backend.streaming import ScoredFlow, StreamingScorer, fuse_tripwire_confidence
from backend.supervised_detector import SupervisedFlowScorer, SupervisedScoredFlow
from cii_calculator import CIIResult, compute_cascading_impact_full
from datasets.asset_registry import AssetRegistry
from datasets.schema import SIGNAL_NETWORK_FLOW
from deception.tripwire import TripwireDetector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: `event_scores.detector` value for the volumetric channel. Matches the
#: registry key in src/detectors/registry.py so the Research Console's
#: benchmark table and the live event_scores rows name the same detector.
DETECTOR_VOLUMETRIC = "isolation_forest"

#: `event_scores.detector` value for the deception channel.
DETECTOR_TRIPWIRE = "tripwire"

#: `event_scores.detector` value for the KNOWN-THREAT channel (Phase B
#: improvement pass) — deliberately NOT the same string as
#: `backend.supervised_detector.DETECTOR_NAME` ("supervised_flow", the
#: key in that module's own `BACKEND_DETECTORS` dict, a different
#: namespace). "random_forest" names the actual algorithm, matching how
#: `DETECTOR_VOLUMETRIC` names "isolation_forest" rather than a role
#: label — readable directly in a raw `event_scores` row without cross-
#: referencing another module's registry.
DETECTOR_SUPERVISED = "random_forest"

#: `event_scores.detector` values for the Hybrid IDS layer
#: (backend/detection/). Re-exported here (not redefined) from
#: backend.detection.contracts, which is where the hybrid layer's own
#: detector names actually live — this module is a consumer of that
#: package, not its source of truth. DETECTOR_VOLUMETRIC / _TRIPWIRE /
#: _SUPERVISED above stay defined in THIS module because they predate the
#: hybrid layer and other code already imports them from here.
_HYBRID_DETECTOR_NAMES = (DETECTOR_SIGNATURE, DETECTOR_BEACONING, DETECTOR_TGNN, DETECTOR_HYBRID)

#: WebSocket envelope types (docs/PHASE5_BUILD_PLAN.md section 7). Ticket
#: #9 owns the transport; this module owns the payloads it carries.
ENVELOPE_EVENT = "event"
ENVELOPE_ALERT = "alert"
ENVELOPE_CII = "cii"
#: The IPS layer's own envelope (backend/ips/). Broadcast for every
#: APPROVED prevention decision (ALERT/RATE_LIMIT/BLOCK/QUARANTINE — never
#: OBSERVE, mirrored from what `_apply_ips_action` persists), plus one
#: more time on automatic TTL expiry and on a manual rollback via
#: POST /api/ips/actions/{id}/rollback — see `_ips_action_envelope`.
ENVELOPE_IPS_ACTION = "ips_action"

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

#: Titles are stable strings, not f-strings over asset names, so the
#: frontend can group/filter on them without parsing prose.
TITLE_TRIPWIRE = "Honeytoken credential used"
TITLE_VOLUMETRIC = "Anomalous traffic volume"
#: Title for an alert that exists ONLY because the hybrid layer's fused
#: decision cleared the alert band while neither tripwire nor volumetric
#: alone did -- see `_hybrid_alert_decision` and `hybrid_gates_alerts`.
TITLE_HYBRID = "Hybrid detection: correlated signal"


# ---------------------------------------------------------------------------
# Broadcast interface
# ---------------------------------------------------------------------------


class Broadcaster(Protocol):
    """Anything that can push a typed envelope to connected clients.

    Ticket #9 supplies the real WebSocket implementation; this ticket
    defines the shape and the payloads. Kept a Protocol rather than a base
    class so the WS layer need not import this module (which would create
    an ingest <-> transport import cycle).
    """

    def publish(self, envelope: dict[str, Any]) -> None:  # pragma: no cover
        ...


class NullBroadcaster:
    """Default broadcaster: drops everything.

    Lets the ingest pipeline be constructed, tested, and run end-to-end
    against Postgres before Ticket #9 exists, without a `None` check at
    every call site.
    """

    def publish(self, envelope: dict[str, Any]) -> None:
        return None


class CollectingBroadcaster:
    """Broadcaster that records envelopes in memory. Tests and diagnostics.

    Bounded by `max_entries` so a long run under this broadcaster cannot
    exhaust memory the way an unbounded list would.
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        self.max_entries = max_entries
        self.envelopes: list[dict[str, Any]] = []

    def publish(self, envelope: dict[str, Any]) -> None:
        self.envelopes.append(envelope)
        if len(self.envelopes) > self.max_entries:
            del self.envelopes[: len(self.envelopes) - self.max_entries]

    def of_type(self, envelope_type: str) -> list[dict[str, Any]]:
        return [e for e in self.envelopes if e.get("type") == envelope_type]


# ---------------------------------------------------------------------------
# Result / stats types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchResult:
    """Outcome of ingesting one micro-batch.

    `events_inserted` can be less than `flows_received` without anything
    being wrong: the D4 unique constraint
    (`replay_session_id`, `source_row_id`) makes a re-delivered batch a
    no-op rather than a duplicate, and `events_deduplicated` counts exactly
    those rows. The two always sum to `flows_received`.
    """

    flows_received: int
    events_inserted: int
    events_deduplicated: int
    anomalies: int
    tripwire_hits: int
    cii_computed: int
    cii_reused: int
    alerts_created: int
    alerts_suppressed: int
    hybrid_signature_hits: int = 0
    hybrid_beaconing_hits: int = 0
    hybrid_tgnn_hits: int = 0
    hybrid_likely_or_above: int = 0
    hybrid_gated_alerts: int = 0
    #: IPS (backend/ips/) per-batch counters. All zero when
    #: ips_enabled=False, or whenever `fused_decisions` is None (the IPS
    #: layer consumes Hybrid IDS output — see backend/ips/policy.py's
    #: module docstring — so it never runs without it, independent of
    #: ips_enabled's own value).
    ips_decisions: int = 0
    ips_actions_enforced: int = 0
    ips_actions_simulated: int = 0
    ips_actions_duplicate_suppressed: int = 0
    ips_actions_escalated: int = 0
    ips_actions_failed: int = 0
    ips_actions_expired: int = 0


@dataclass
class IngestStats:
    """Cumulative counters across every batch this pipeline has ingested.

    Ticket #16 (`/api/stats`) is the intended consumer. Mutated under
    `IngestPipeline._lock`, so read it via `IngestPipeline.stats()` which
    returns a snapshot rather than the live object.
    """

    batches: int = 0
    flows_received: int = 0
    events_inserted: int = 0
    events_deduplicated: int = 0
    anomalies: int = 0
    tripwire_hits: int = 0
    cii_computed: int = 0
    cii_reused: int = 0
    alerts_created: int = 0
    alerts_suppressed: int = 0
    broadcast_failures: int = 0
    events_pruned: int = 0
    #: Hybrid IDS (backend/detection/) cumulative counters. All zero when
    #: hybrid_enabled=False, or on a pipeline built before this field
    #: existed replaying into a fresh IngestStats() -- these are purely
    #: additive telemetry, never read by the existing alert/CII path.
    hybrid_signature_hits: int = 0
    hybrid_beaconing_hits: int = 0
    hybrid_tgnn_hits: int = 0
    hybrid_likely_or_above: int = 0
    hybrid_gated_alerts: int = 0
    ips_decisions: int = 0
    ips_actions_enforced: int = 0
    ips_actions_simulated: int = 0
    ips_actions_duplicate_suppressed: int = 0
    ips_actions_escalated: int = 0
    ips_actions_failed: int = 0
    ips_actions_expired: int = 0

    def absorb(self, result: BatchResult) -> None:
        self.batches += 1
        self.flows_received += result.flows_received
        self.events_inserted += result.events_inserted
        self.events_deduplicated += result.events_deduplicated
        self.anomalies += result.anomalies
        self.tripwire_hits += result.tripwire_hits
        self.cii_computed += result.cii_computed
        self.cii_reused += result.cii_reused
        self.alerts_created += result.alerts_created
        self.alerts_suppressed += result.alerts_suppressed
        self.hybrid_signature_hits += result.hybrid_signature_hits
        self.hybrid_beaconing_hits += result.hybrid_beaconing_hits
        self.hybrid_tgnn_hits += result.hybrid_tgnn_hits
        self.hybrid_likely_or_above += result.hybrid_likely_or_above
        self.hybrid_gated_alerts += result.hybrid_gated_alerts
        self.ips_decisions += result.ips_decisions
        self.ips_actions_enforced += result.ips_actions_enforced
        self.ips_actions_simulated += result.ips_actions_simulated
        self.ips_actions_duplicate_suppressed += result.ips_actions_duplicate_suppressed
        self.ips_actions_escalated += result.ips_actions_escalated
        self.ips_actions_failed += result.ips_actions_failed
        # ips_actions_expired is NOT absorbed from BatchResult — expiry is
        # driven by wall-clock TTL, not batch volume, and is applied
        # directly against the live self._stats by `_maybe_expire_ips_actions`
        # (mirroring how `events_pruned` is added directly in
        # `ingest_batch`, not threaded through BatchResult either).


@dataclass
class _CacheEntry:
    """One debounce-cache slot: a CII result plus when it was computed."""

    result: CIIResult
    snapshot_id: Optional[int]
    computed_at: float


@dataclass
class _IpsActionState:
    """One active-mitigation registry entry (`IngestPipeline.
    _active_ips_actions`, keyed by target asset).

    `expires_at` is in the SAME clock domain as `self._clock()` (monotonic
    seconds, injectable for tests — matching `_CacheEntry.computed_at`
    and `_last_alert_at`'s values), NOT a wall-clock `datetime`; the
    persisted `IpsAction.expires_at` column is the wall-clock twin of
    this, computed once at creation time from `datetime.now(timezone.utc)
    + timedelta(seconds=ttl_sec)`.
    """

    action: PreventionAction
    action_id: Optional[int]
    expires_at: Optional[float]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def default_tripwire_signal(flow: ReplayFlow) -> bool:
    """Whether `flow` touched a honeytoken credential.

    `ReplayFlow` (Ticket #5b) carries no honeytoken field, and that is
    correct: it is a raw source of real CIC-IDS2017 identifiers (P5-9), and
    replayed 2017 capture traffic never touched an AEGIS honeytoken. So
    this reads an OPTIONAL attribute and defaults to False -- exactly the
    contract `TripwireDetector.features_from_df` already documents for a
    frame with no `is_honeytoken_use` column.

    The hook exists so Ticket #13's `POST /api/inject` can hand the engine
    flows that DO carry the signal (as an attribute, or via a custom
    `tripwire_signal` callable) without modifying `ReplayFlow` and without
    this module growing its own honeytoken-matching logic, which would
    violate Invariant C.
    """
    return bool(getattr(flow, "is_honeytoken_use", False))


def build_criticality_map() -> dict[str, float]:
    """Asset name -> criticality, from the single graph authority.

    Sourced from `backend.seed.compute_seed_rows()`, which is built from
    `graph_manager.build_graph()` (Invariant D). Deliberately NOT read back
    from the `assets` table: a stale or unseeded table would silently
    change CII results, and `seed_assets()` already treats divergence
    between graph and table as a reportable condition rather than a source
    of truth.
    """
    return {row["name"]: float(row["criticality"]) for row in compute_seed_rows()}


def compute_risk_index(
    unacknowledged_counts: Sequence[tuple[str, str, int]],
    criticality_map: dict[str, float],
    settings: Optional[BackendSettings] = None,
) -> int:
    """GET /api/stats' `risk_index` (Ticket #16, decision D16-1)::

        risk = clamp(0, 100) of
               100 * Sum over UNACKNOWLEDGED alerts of
                     (severity_weight x asset_criticality)
               / BACKEND_SETTINGS.risk_index_full_scale

    `unacknowledged_counts` is `(severity, asset, count)` triples -- the
    shape a bounded `GROUP BY severity, asset` aggregate over the alerts
    table naturally produces (see `backend/routes.py`'s `get_stats`), so
    a table with a large history of alert rows still costs one small
    aggregate query, never a per-row fetch into Python.

    `severity_weight` comes from `BACKEND_SETTINGS.risk_severity_weight_*`
    (falling back to `risk_severity_weight_default` for any severity
    string other than "critical"/"warning" -- see that field's docstring).
    `asset_criticality` comes from `criticality_map`
    (`build_criticality_map()` above -- the one graph authority,
    Invariant D), defaulting to 0.0 for an asset that is not a graph node.
    That is a real case, not a hypothetical: an auto-registered
    `Unresolved_<ip>` asset (risk T5) can legitimately have an alert --
    `_compute_or_reuse_cii` above still raises one, just without a CII
    snapshot -- and there is no real criticality basis for such an asset,
    so it contributes nothing to the index rather than a fabricated guess.

    UNACKNOWLEDGED ONLY, deliberately. Acknowledging an alert must
    visibly lower this number -- that is what makes it an operator tool
    ("I've seen this, it's handled") rather than a decoration that never
    changes once painted. An empty `unacknowledged_counts` returns `0`,
    never `None`: zero unacknowledged alerts is a real, meaningful
    "nothing is outstanding" state, distinct from "no basis to compute"
    (reserved for when there is no replay engine at all -- see
    `_require_replay_engine` in `backend/routes.py`).

    Deliberately NOT built on CII (D16-1): measured across all 50 assets
    in `config.SMART_CITY_ASSETS` this session, CII is currently
    near-binary -- 28 report exactly 0.0, 18 exactly 1.0, only 4 in
    between -- and feeding that degeneracy into the first number an
    operator reads would propagate it into the headline figure. See
    docs/PHASE5_TICKET16_PLAN.md section 3.

    `risk_index_full_scale` is a PRESENTATION SCALE, not a calibrated
    probability (see that field's docstring in `backend/config.py`) --
    there is no ground truth for "100% risk" in this system, only a
    chosen denominator that keeps the number legible.
    """
    s = settings if settings is not None else BACKEND_SETTINGS
    weights = {
        SEVERITY_CRITICAL: s.risk_severity_weight_critical,
        SEVERITY_WARNING: s.risk_severity_weight_warning,
    }
    total = 0.0
    for severity, asset, count in unacknowledged_counts:
        weight = weights.get(severity, s.risk_severity_weight_default)
        total += weight * criticality_map.get(asset, 0.0) * count
    scaled = 100.0 * total / s.risk_index_full_scale
    return int(round(max(0.0, min(100.0, scaled))))


def _jsonable(value: Any) -> Any:
    """Coerce numpy scalars to plain Python for JSONB serialisation.

    psycopg cannot adapt `np.float64`/`np.bool_` into JSONB -- the same
    constraint `StreamingScorer.explain()` documents. Applied to anything
    derived from a `CIIResult`, whose `hop_details` values come out of
    numpy arithmetic.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _ips_action_envelope(row: IpsAction) -> dict[str, Any]:
    """Build an `ips_action` WebSocket envelope from a persisted (and
    flushed -- `row.id` must already be set) `IpsAction` row. Shared by
    every call site that broadcasts one: a freshly-created action
    (`_apply_ips_action`), an expired one (`_maybe_expire_ips_actions`),
    and a manually rolled-back one (`POST /api/ips/actions/{id}/rollback`
    in backend/routes.py), so the shape is identical regardless of why
    the row changed -- same "split out to avoid writing the dict literal
    twice" rationale as `_publish_cii_envelope`.
    """
    return {
        "type": ENVELOPE_IPS_ACTION,
        "data": {
            "id": row.id,
            "ts": row.ts.isoformat(),
            "target_asset": row.target_asset,
            "action": row.action,
            "status": row.status,
            "reason": row.reason,
            "evidence": row.evidence,
            "confidence": row.confidence,
            "dry_run": row.dry_run,
            "triggering_event_id": row.triggering_event_id,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "rolled_back_at": row.rolled_back_at.isoformat() if row.rolled_back_at else None,
            "rollback_reason": row.rollback_reason,
        },
    }


# ---------------------------------------------------------------------------
# IngestPipeline
# ---------------------------------------------------------------------------


class IngestPipeline:
    """Replay consumer: scores, persists, and broadcasts each micro-batch.

    Parameters
    ----------
    scorer:
        A fitted `StreamingScorer`. Required -- this pipeline never fits a
        model (Invariant B). Build the artifact with
        `python -m backend.warmup` and load it with
        `StreamingScorer.load()`.
    broadcaster:
        Where typed envelopes go. Defaults to `NullBroadcaster` so the
        pipeline runs end-to-end before Ticket #9 lands.
    supervised_scorer:
        Optional fitted `SupervisedFlowScorer` (Phase B improvement pass)
        — the KNOWN-THREAT channel. Unlike `scorer`, `None` is a fully
        supported, common state (no artifact built yet): every event
        still gets a volumetric + tripwire verdict exactly as before this
        parameter existed; a `detector="random_forest"` row is simply
        never written to `event_scores` for that event. Purely additive
        and purely informational — it does NOT participate in
        `fuse_tripwire_confidence()` or the alert/suppression policy, so
        wiring it can never change `is_anomaly`, `alerts_created`, or any
        other already-measured/published statistic.
    registry:
        Identifier -> asset resolver. Defaults to a fresh `AssetRegistry`.
        Pass a shared instance to keep auto-discovered assets consistent
        across pipelines.
    criticality_map:
        Optional override; defaults to `build_criticality_map()`.
    tripwire_signal:
        Optional override; defaults to `default_tripwire_signal`.
    cii_debounce_sec, cii_cache_max_entries, alert_on_volumetric,
    alert_volumetric_min_calibrated_score, alert_asset_debounce_sec,
    retention_check_every_n_batches:
        Optional overrides for the matching `BACKEND_SETTINGS` fields,
        following the repo's optional-override convention (CLAUDE.md
        section 5) so tests and sweeps need not mutate global config.
    session_factory:
        Optional context-manager factory used instead of
        `backend.db.session_scope`, for tests that bind to a throwaway
        database.
    clock:
        Monotonic seconds source for the debounce windows. Injectable so
        debounce tests need no `sleep()`.
    ips_enabled, ips_dry_run, ips_active_action_cache_max_entries:
        Optional overrides for the matching `BACKEND_SETTINGS.ips_*`
        fields (backend/ips/). `ips_enabled=False` (the default) makes
        the IPS layer a complete no-op, mirroring `hybrid_enabled`'s role
        for the Hybrid IDS layer — see backend/ips/policy.py's module
        docstring.
    policy_engine, enforcement_adapter:
        Optional injected `IPSPolicyEngine` / `EnforcementAdapter`
        (backend/ips/), same injectable-optional-override pattern as
        `signature_engine`/`beaconing_detector`/`fusion_engine` above —
        a fresh default instance of each is constructed when omitted.
    """

    def __init__(
        self,
        scorer: StreamingScorer,
        broadcaster: Optional[Broadcaster] = None,
        supervised_scorer: Optional[SupervisedFlowScorer] = None,
        registry: Optional[AssetRegistry] = None,
        criticality_map: Optional[dict[str, float]] = None,
        tripwire_signal: Optional[Callable[[ReplayFlow], bool]] = None,
        cii_debounce_sec: Optional[float] = None,
        cii_cache_max_entries: Optional[int] = None,
        alert_on_volumetric: Optional[bool] = None,
        alert_volumetric_min_calibrated_score: Optional[float] = None,
        alert_asset_debounce_sec: Optional[float] = None,
        retention_check_every_n_batches: Optional[int] = None,
        session_factory: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        hybrid_enabled: Optional[bool] = None,
        hybrid_gates_alerts: Optional[bool] = None,
        signature_engine: Optional[SignatureEngine] = None,
        beaconing_detector: Optional[BeaconingDetector] = None,
        tgnn_detector: Optional[TGNNDetector] = None,
        fusion_engine: Optional[HybridFusionEngine] = None,
        ips_enabled: Optional[bool] = None,
        ips_dry_run: Optional[bool] = None,
        ips_active_action_cache_max_entries: Optional[int] = None,
        policy_engine: Optional[IPSPolicyEngine] = None,
        enforcement_adapter: Optional[EnforcementAdapter] = None,
    ) -> None:
        if scorer is None:
            raise ValueError("IngestPipeline requires a fitted StreamingScorer")

        self._scorer = scorer
        self._supervised_scorer = supervised_scorer
        self._broadcaster: Broadcaster = broadcaster or NullBroadcaster()
        # from_config(), NOT AssetRegistry(): the bare constructor builds an
        # EMPTY registry, so every curated smart-city IP would fall through
        # to the auto-discovery branch and resolve to Unresolved_<ip>. That
        # failure is silent — events still persist, the feed still scrolls —
        # but every asset name is wrong, every CII is zero (an Unresolved_
        # node has no dependency-graph edges), and no alert can ever name a
        # real asset. from_config() seeds SMART_CITY_ASSETS and
        # EXTERNAL_THREAT_IPS.
        self._registry = registry if registry is not None else AssetRegistry.from_config()
        self._criticality_map = (
            criticality_map if criticality_map is not None else build_criticality_map()
        )
        self._tripwire_signal = tripwire_signal or default_tripwire_signal
        self._tripwire = TripwireDetector()

        settings = BACKEND_SETTINGS
        self._cii_debounce_sec = (
            settings.cii_debounce_sec if cii_debounce_sec is None else cii_debounce_sec
        )
        self._cii_cache_max_entries = (
            settings.cii_cache_max_entries
            if cii_cache_max_entries is None
            else cii_cache_max_entries
        )
        self._alert_on_volumetric = (
            settings.alert_on_volumetric if alert_on_volumetric is None else alert_on_volumetric
        )
        self._alert_volumetric_min_calibrated_score = (
            settings.alert_volumetric_min_calibrated_score
            if alert_volumetric_min_calibrated_score is None
            else alert_volumetric_min_calibrated_score
        )
        self._alert_asset_debounce_sec = (
            settings.alert_asset_debounce_sec
            if alert_asset_debounce_sec is None
            else alert_asset_debounce_sec
        )
        self._retention_check_every_n_batches = (
            settings.ingest_retention_check_every_n_batches
            if retention_check_every_n_batches is None
            else retention_check_every_n_batches
        )

        self._session_factory = session_factory or session_scope
        self._clock = clock or time.monotonic

        # ---- Hybrid IDS (backend/detection/) ---------------------------
        # `hybrid_enabled` gates whether the three extra detectors run at
        # all -- False reproduces pre-hybrid behaviour exactly (no signature
        # or beaconing verdicts, no "hybrid" event_scores row, no hybrid
        # field in the broadcast envelope). `hybrid_gates_alerts` is a
        # SEPARATE, narrower switch: even with the layer enabled and
        # observing every batch, it may not create an alert the existing
        # tripwire/volumetric policy would not have created until this is
        # also true -- see `_hybrid_alert_decision`'s docstring. Both
        # default from BACKEND_SETTINGS (hybrid_enabled=True,
        # hybrid_gates_alerts=False), so the layer ships OBSERVABLE by
        # default but not yet authoritative over alerting.
        self._hybrid_enabled = (
            settings.hybrid_enabled if hybrid_enabled is None else hybrid_enabled
        )
        self._hybrid_gates_alerts = (
            settings.hybrid_gates_alerts if hybrid_gates_alerts is None else hybrid_gates_alerts
        )
        # `BeaconingDetector` is STATEFUL (per-pair inter-arrival history
        # spanning batches), so it must be one long-lived instance held on
        # `self`, never rebuilt per batch -- rebuilding it would silently
        # reset every pair's history every batch and the detector would
        # never accumulate enough samples to leave its abstain state.
        # `SignatureEngine` and `HybridFusionEngine` are stateless/pure, so
        # a fresh instance vs. an injected one behaves identically; they are
        # still held on `self` so a caller can inject a test double for
        # either the same way `scorer`/`supervised_scorer` are injected.
        # `TGNNDetector` is STATEFUL like `BeaconingDetector` (an
        # accumulating communication graph plus a lazily-fitted baseline
        # spanning batches), so it holds the same long-lived-instance
        # requirement -- see `TGNNDetector`'s class docstring.
        self._signature_engine = signature_engine or SignatureEngine()
        self._beaconing_detector = beaconing_detector or BeaconingDetector()
        self._tgnn_detector = tgnn_detector or TGNNDetector()
        self._fusion_engine = fusion_engine or HybridFusionEngine()

        # ---- IPS (backend/ips/) -----------------------------------------
        # Same optional-override / BACKEND_SETTINGS-fallback pattern as
        # the Hybrid IDS block above. `ips_enabled=False` by default
        # (unlike `hybrid_enabled=True`) — see backend/config.py's
        # `ips_enabled` docstring for why this layer ships opt-in rather
        # than on-but-advisory. `IPSPolicyEngine` and
        # `SimulatedEnforcementAdapter` are both stateless/pure, so a
        # fresh instance vs. an injected one behaves identically — held
        # on `self` so tests can inject a double the same way
        # `fusion_engine` already is.
        self._ips_enabled = settings.ips_enabled if ips_enabled is None else ips_enabled
        self._ips_dry_run = settings.ips_dry_run if ips_dry_run is None else ips_dry_run
        self._ips_active_action_cache_max_entries = (
            settings.ips_active_action_cache_max_entries
            if ips_active_action_cache_max_entries is None
            else ips_active_action_cache_max_entries
        )
        self._policy_engine = policy_engine or IPSPolicyEngine()
        self._enforcement_adapter = enforcement_adapter or SimulatedEnforcementAdapter()
        #: Active-mitigation registry, keyed by target asset — mirrors
        #: `_cii_cache`/`_last_alert_at`'s bounded-OrderedDict shape
        #: exactly. This is the ONLY place duplicate/conflicting-action
        #: protection and TTL expiry are decided; `IPSPolicyEngine`
        #: itself is stateless and knows nothing about it (see that
        #: module's docstring).
        self._active_ips_actions: "OrderedDict[str, _IpsActionState]" = OrderedDict()

        # The engine drives the consumer from its own replay thread, while
        # Ticket #16's /api/stats reads counters from a request thread.
        self._lock = threading.Lock()
        self._stats = IngestStats()
        self._cii_cache: "OrderedDict[str, _CacheEntry]" = OrderedDict()
        self._last_alert_at: "OrderedDict[str, float]" = OrderedDict()

    # ------------------------------------------------------------------
    # Consumer entry point
    # ------------------------------------------------------------------

    def __call__(self, batch: list, meta: BatchMeta) -> BatchResult:
        """Ingest one micro-batch. Matches `replay_engine.Consumer`.

        Returns a `BatchResult` for tests and direct callers; the engine
        ignores the return value. Raises on persistence failure so the
        engine's `consumer_error_count` stays honest -- see the module
        docstring, "Failure semantics".
        """
        return self.ingest_batch(batch, meta)

    def ingest_batch(self, batch: Sequence[ReplayFlow], meta: BatchMeta) -> BatchResult:
        flows = list(batch)
        if not flows:
            empty = BatchResult(0, 0, 0, 0, 0, 0, 0, 0, 0)
            with self._lock:
                self._stats.absorb(empty)
            return empty

        # ---- 1-3. score, tripwire, fuse -------------------------------
        scored = self._scorer.score_batch(flows)
        tripwire_fired = self._tripwire_flags(flows)
        volume_fired = np.array([s.is_anomaly for s in scored], dtype=bool)
        is_anomaly, confidence = fuse_tripwire_confidence(volume_fired, tripwire_fired)
        # Phase B improvement pass: the KNOWN-THREAT channel, purely
        # additive — never folded into `is_anomaly`/`confidence` above,
        # so its presence can never change the alert/suppression policy
        # or any already-published statistic derived from it.
        supervised_scored = (
            self._supervised_scorer.score_batch(flows)
            if self._supervised_scorer is not None
            else None
        )

        # ---- 3b. Hybrid IDS (backend/detection/) -----------------------
        # Fully additive and independently switchable (hybrid_enabled):
        # runs the signature + beaconing detectors, adapts every existing
        # channel's own verdict via contracts.verdict_from_*, and fuses
        # all of it into one FusedDecision per flow. `fused_decisions` is
        # None when the layer is off, and every downstream method below
        # treats None as "behave exactly as before this layer existed" --
        # see each method's own hybrid-related parameter docstring.
        fused_decisions = (
            self._compute_hybrid_decisions(flows, scored, tripwire_fired, supervised_scored)
            if self._hybrid_enabled
            else None
        )

        # ---- 4. resolve identities ------------------------------------
        resolutions = [
            (
                self._registry.resolve(flow.source_ip),
                self._registry.resolve(flow.destination_ip),
            )
            for flow in flows
        ]

        # ---- 5-7. persist, CII, alert (one transaction) ---------------
        with self._session_factory() as session:
            inserted_ids, deduplicated = self._persist_events(
                session, flows, resolutions, meta
            )
            self._persist_scores(
                session, scored, inserted_ids, tripwire_fired, is_anomaly, confidence,
                supervised_scored=supervised_scored,
                fused_decisions=fused_decisions,
            )
            cii_outcomes, alert_outcomes, ips_outcomes = self._handle_anomalies(
                session, scored, inserted_ids, resolutions, tripwire_fired, is_anomaly, meta,
                fused_decisions=fused_decisions,
            )
            # IPS TTL sweep: cheap (in-memory registry, not a DB scan —
            # see its docstring), so it runs every batch rather than being
            # gated behind `_retention_check_every_n_batches` like
            # `_maybe_prune`. Skipped entirely when the layer is off,
            # matching every other IPS call site's `self._ips_enabled`
            # guard.
            expired_envelopes = (
                self._maybe_expire_ips_actions(session) if self._ips_enabled else []
            )
            pruned = self._maybe_prune(session)

        # ---- 8. broadcast (AFTER commit) ------------------------------
        self._broadcast_batch(
            scored, inserted_ids, resolutions, tripwire_fired, is_anomaly, confidence, meta,
            fused_decisions=fused_decisions,
        )
        for envelope in (
            cii_outcomes.envelopes
            + alert_outcomes.envelopes
            + ips_outcomes.envelopes
            + expired_envelopes
        ):
            self._safe_publish(envelope)

        hybrid_signature_hits = 0
        hybrid_beaconing_hits = 0
        hybrid_tgnn_hits = 0
        hybrid_likely_or_above = 0
        if fused_decisions is not None:
            for decision in fused_decisions:
                for verdict in decision.verdicts:
                    if verdict.fired and verdict.detector == DETECTOR_SIGNATURE:
                        hybrid_signature_hits += 1
                    elif verdict.fired and verdict.detector == DETECTOR_BEACONING:
                        hybrid_beaconing_hits += 1
                    elif verdict.fired and verdict.detector == DETECTOR_TGNN:
                        hybrid_tgnn_hits += 1
                if decision.action == ResponseAction.ALERT:
                    hybrid_likely_or_above += 1

        result = BatchResult(
            flows_received=len(flows),
            events_inserted=sum(1 for i in inserted_ids if i is not None),
            events_deduplicated=deduplicated,
            anomalies=int(is_anomaly.sum()),
            tripwire_hits=int(tripwire_fired.sum()),
            cii_computed=cii_outcomes.computed,
            cii_reused=cii_outcomes.reused,
            alerts_created=alert_outcomes.created,
            alerts_suppressed=alert_outcomes.suppressed,
            hybrid_signature_hits=hybrid_signature_hits,
            hybrid_beaconing_hits=hybrid_beaconing_hits,
            hybrid_tgnn_hits=hybrid_tgnn_hits,
            hybrid_likely_or_above=hybrid_likely_or_above,
            hybrid_gated_alerts=alert_outcomes.hybrid_gated,
            ips_decisions=ips_outcomes.decisions,
            ips_actions_enforced=ips_outcomes.enforced,
            ips_actions_simulated=ips_outcomes.simulated,
            ips_actions_duplicate_suppressed=ips_outcomes.duplicate_suppressed,
            ips_actions_escalated=ips_outcomes.escalated,
            ips_actions_failed=ips_outcomes.failed,
        )
        with self._lock:
            self._stats.absorb(result)
            self._stats.events_pruned += pruned
        return result

    # ------------------------------------------------------------------
    # Stage 2 — tripwire
    # ------------------------------------------------------------------

    def _tripwire_flags(self, flows: Sequence[ReplayFlow]) -> np.ndarray:
        """Run the REAL `TripwireDetector` over the batch (Invariant C).

        Builds the one-column frame `features_from_df` expects and reads
        its sklearn-convention `predict()` output (-1 anomaly / 1 normal).
        Deliberately routed through the detector rather than reading the
        booleans directly, so that any future change to what counts as a
        honeytoken touch takes effect here with no edit to this module.
        """
        df = pd.DataFrame(
            {"is_honeytoken_use": [self._tripwire_signal(f) for f in flows]}
        )
        X = TripwireDetector.features_from_df(df)
        return self._tripwire.predict(X) == -1

    # ------------------------------------------------------------------
    # Stage 3b — Hybrid IDS (backend/detection/)
    # ------------------------------------------------------------------

    def _compute_hybrid_decisions(
        self,
        flows: Sequence[ReplayFlow],
        scored: Sequence[ScoredFlow],
        tripwire_fired: np.ndarray,
        supervised_scored: Optional[Sequence[SupervisedScoredFlow]],
    ) -> list[FusedDecision]:
        """Run every hybrid-layer detector and fuse their verdicts, one
        `FusedDecision` per flow, in batch order.

        This is the ONLY place `ReplayFlow` is projected into
        `FlowFeatures` -- every detector downstream of this method sees
        only the label-free view (see `contracts.py`'s module docstring
        for why that projection exists and what it deliberately omits).

        Every existing channel's own verdict is included via the
        `contracts.verdict_from_*` adapters, NOT recomputed: the
        volumetric and tripwire channels already ran in stages 1-2 above,
        and the supervised channel (if loaded) already ran alongside them.
        Reusing their output rather than re-deriving it means the hybrid
        layer can never disagree with the very numbers it is fusing.
        """
        features = [FlowFeatures.from_replay_flow(flow) for flow in flows]

        signature_verdicts = (
            self._signature_engine.examine(features)
            if BACKEND_SETTINGS.signature_enabled
            else [None] * len(features)
        )
        beaconing_verdicts = (
            self._beaconing_detector.examine(features)
            if BACKEND_SETTINGS.beaconing_enabled
            else [None] * len(features)
        )
        tgnn_verdicts = (
            self._tgnn_detector.examine(features)
            if BACKEND_SETTINGS.tgnn_enabled
            else [None] * len(features)
        )

        settings = BACKEND_SETTINGS
        decisions: list[FusedDecision] = []
        for i, scored_flow in enumerate(scored):
            verdicts: list[DetectorVerdict] = [
                verdict_from_scored_flow(
                    scored_flow, settings.hybrid_weight_volumetric, DETECTOR_VOLUMETRIC
                ),
                verdict_from_tripwire(
                    bool(tripwire_fired[i]),
                    self._tripwire.tripwire_score,
                    settings.hybrid_weight_tripwire,
                    DETECTOR_TRIPWIRE,
                ),
            ]
            if supervised_scored is not None:
                verdicts.append(
                    verdict_from_supervised(
                        supervised_scored[i], settings.hybrid_weight_supervised, DETECTOR_SUPERVISED
                    )
                )
            if signature_verdicts[i] is not None:
                verdicts.append(signature_verdicts[i])
            if beaconing_verdicts[i] is not None:
                verdicts.append(beaconing_verdicts[i])
            if tgnn_verdicts[i] is not None:
                verdicts.append(tgnn_verdicts[i])

            decisions.append(self._fusion_engine.fuse(verdicts))
        return decisions

    def _hybrid_alert_decision(
        self,
        fused: FusedDecision,
        origin_asset: str,
    ) -> tuple[bool, str]:
        """Should the HYBRID layer, on its own authority, trigger an
        alert this flow's existing tripwire/volumetric verdict did not?

        Deliberately a SEPARATE decision from `_alert_decision` rather
        than folded into it: `_alert_decision` gates on the volumetric
        channel's OWN `calibrated_score` against
        `alert_volumetric_min_calibrated_score`, a threshold calibrated
        for that channel's score distribution. The fused `threat_score`
        lives on a different, band-thresholded scale (noisy-OR over
        multiple weighted detectors) -- reusing the volumetric floor
        against it would compare two numbers that are not the same kind
        of quantity.

        Callable ONLY for a flow the existing tripwire/volumetric path
        did NOT already alert on -- `_handle_anomalies`'s `if
        is_anomaly[i]: ... else: self._hybrid_alert_decision(...)` split
        is what guarantees that, and is therefore this method's real
        double-alert guard, not a check inside this method. (An earlier
        revision carried a redundant `already_anomaly` parameter here
        that could never actually be True given that single call site --
        removed rather than left as unreachable code that implied a
        check this method does not itself need to make. Pinned by
        tests/test_ingest_hybrid.py::
        test_hybrid_never_double_alerts_when_tripwire_already_fired,
        which asserts against the ACTUAL double-alert outcome, not
        against this method's internals.) This also means a CONFIRMED
        tripwire signal -- which always sets `is_anomaly[i]` via
        `fuse_tripwire_confidence` -- can never reach this method at all:
        the hybrid layer structurally cannot duplicate that alert.

        Gated behind `hybrid_gates_alerts` (default False) -- see that
        setting's docstring in `backend/config.py` for why this ships
        off by default: turning it on changes which flows can create an
        alert at all, and every alert/risk figure already published in
        this project was measured under the pre-hybrid policy.

        `origin_asset` is used ONLY for the shared debounce check
        (`_debounce_ok`), applied last so it is never touched by a
        decision this method is about to reject on other grounds.
        """
        if not self._hybrid_gates_alerts:
            return False, "hybrid_gates_alerts disabled"
        if fused.action != ResponseAction.ALERT:
            return False, f"fused band {fused.band.value} below alert threshold"
        # Shared debounce state with `_alert_decision` (see
        # `_debounce_ok`'s docstring) -- checked LAST, matching
        # `_alert_decision`'s own ordering, since it has the side effect
        # of recording a touch and must only do so once every other
        # check has already passed.
        return self._debounce_ok(origin_asset)

    # ------------------------------------------------------------------
    # Stage 5 — persistence
    # ------------------------------------------------------------------

    def _persist_events(
        self,
        session: Any,
        flows: Sequence[ReplayFlow],
        resolutions: list[tuple[Any, Any]],
        meta: BatchMeta,
    ) -> tuple[list[Optional[int]], int]:
        """Bulk-insert events, preserving batch order; return their ids.

        Uses `ON CONFLICT DO NOTHING` on the D4 unique constraint
        (`replay_session_id`, `source_row_id`) so a re-delivered batch --
        an ingest retry after a partial failure -- is a no-op instead of a
        duplicate-key crash, which is precisely what that constraint was
        added for.

        Rows are inserted in the order the engine emitted them, so the
        serial `id` preserves true emission order within a tied `ts`. The
        state board's "Note for Ticket #8" depends on this: with hundreds
        of events sharing one minute-granularity timestamp (friday-morning
        peaks at 4,017), `ORDER BY ts DESC, id DESC` is the only stable
        ordering for the live feed, and it is only meaningful if ingest
        inserts in arrival order.

        Returns `(ids, deduplicated_count)` where `ids` is positionally
        aligned to `flows` and holds `None` for a row that already existed.
        """
        rows = []
        for flow, (src_res, dst_res) in zip(flows, resolutions):
            rows.append(
                {
                    # D5: three distinct timestamps. `ts` is event time
                    # from the dataset; `observed_at` is detection time
                    # (when this pipeline saw it); `ingested_at` is the
                    # server-side now(). Never collapsed.
                    "ts": flow.ts,
                    "observed_at": meta.emitted_at,
                    "source_id": flow.source_ip,
                    "destination_id": flow.destination_ip,
                    "source_asset": src_res.asset_name,
                    "destination_asset": dst_res.asset_name,
                    "protocol": flow.protocol,
                    "bytes": flow.bytes,
                    "packets": flow.packets,
                    "duration_sec": flow.duration_sec,
                    "signal_type": SIGNAL_NETWORK_FLOW,
                    "source_dataset": flow.source_dataset,
                    "timing_provenance": flow.timing_provenance,
                    "replay_session_id": meta.replay_session_id,
                    "source_row_id": flow.source_row_id,
                    "raw": {
                        "source_port": flow.source_port,
                        "destination_port": flow.destination_port,
                        "label": flow.label,
                        "is_attack": bool(flow.is_attack),
                        "batch_index": meta.batch_index,
                        "batch_origin": meta.origin,
                        "source_confidence": float(src_res.confidence),
                        "destination_confidence": float(dst_res.confidence),
                    },
                }
            )

        stmt = (
            pg_insert(Event)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_events_replay_session_source_row")
            .returning(Event.id, Event.source_row_id)
        )
        returned = session.execute(stmt).all()
        # RETURNING with ON CONFLICT DO NOTHING yields only the rows that
        # were actually inserted, so this map is exactly the non-duplicate
        # subset. Map back by source_row_id to stay positionally aligned.
        id_by_row = {row_id: ev_id for ev_id, row_id in returned}
        ids: list[Optional[int]] = [id_by_row.get(f.source_row_id) for f in flows]
        deduplicated = sum(1 for i in ids if i is None)
        if deduplicated:
            logger.debug(
                "ingest: %d/%d flows in batch %d already present "
                "(replay_session_id=%s) — deduplicated per D4",
                deduplicated,
                len(flows),
                meta.batch_index,
                meta.replay_session_id,
            )
        return ids, deduplicated

    def _persist_scores(
        self,
        session: Any,
        scored: Sequence[ScoredFlow],
        inserted_ids: Sequence[Optional[int]],
        tripwire_fired: np.ndarray,
        is_anomaly: np.ndarray,
        confidence: np.ndarray,
        supervised_scored: Optional[Sequence[SupervisedScoredFlow]] = None,
        fused_decisions: Optional[Sequence[FusedDecision]] = None,
    ) -> None:
        """Insert one volumetric score row per event, plus a tripwire row
        only where the tripwire actually fired, plus (Phase B improvement
        pass) one KNOWN-THREAT row per event when `supervised_scored` is
        supplied (i.e. `AppRuntime.supervised_scorer` loaded successfully).

        A tripwire row for every event would double `event_scores` volume
        to record "no honeytoken was touched", which is the default state
        of every ordinary flow and carries no information. Where it DID
        fire, the row is written so an operator can see the deception
        channel's verdict alongside the volumetric one.

        `supervised_scored[i]`'s own `confidence` is that channel's own
        `calibrated_score` (a native `P(attack)`), NOT the fused
        `confidence[i]` the volumetric/tripwire rows use — this channel
        was never part of that fusion (see `ingest_batch`'s comment) and
        must not borrow a confidence value that reflects a different
        detector's verdict.

        `fused_decisions[i]` (Hybrid IDS), when supplied, writes ONE
        `event_scores` row per event under `DETECTOR_HYBRID` -- always,
        like the volumetric row, not only when it fired, because it is
        the pipeline's one HEADLINE per-event verdict across every
        channel and an operator filtering/sorting `event_scores` should
        be able to rely on it existing for every event. `raw_score` is
        left `None` (there is no single native unit for a fused figure);
        `calibrated_score` and `confidence` both carry
        `fused_decisions[i].threat_score` -- deliberately the SAME value
        in both columns, so a caller reading either gets the fused
        figure rather than accidentally reading a different channel's
        `confidence[i]` under the `hybrid` detector name.
        """
        rows: list[dict[str, Any]] = []
        for i, scored_flow in enumerate(scored):
            event_id = inserted_ids[i]
            if event_id is None:
                # Deduplicated event: its scores already exist from the
                # first delivery. Writing them again would create orphaned
                # duplicate score rows against the original event.
                continue
            rows.append(
                {
                    "event_id": event_id,
                    "detector": DETECTOR_VOLUMETRIC,
                    "raw_score": scored_flow.raw_score,
                    "calibrated_score": scored_flow.calibrated_score,
                    "is_anomaly": bool(scored_flow.is_anomaly),
                    "confidence": float(confidence[i]),
                }
            )
            if tripwire_fired[i]:
                rows.append(
                    {
                        "event_id": event_id,
                        "detector": DETECTOR_TRIPWIRE,
                        "raw_score": float(self._tripwire.tripwire_score),
                        "calibrated_score": 1.0,
                        "is_anomaly": True,
                        "confidence": float(confidence[i]),
                    }
                )
            if supervised_scored is not None:
                sup = supervised_scored[i]
                rows.append(
                    {
                        "event_id": event_id,
                        "detector": DETECTOR_SUPERVISED,
                        "raw_score": sup.raw_score,
                        "calibrated_score": sup.calibrated_score,
                        "is_anomaly": bool(sup.is_anomaly),
                        "confidence": sup.calibrated_score,
                    }
                )
            if fused_decisions is not None:
                decision = fused_decisions[i]
                rows.append(
                    {
                        "event_id": event_id,
                        "detector": DETECTOR_HYBRID,
                        "raw_score": None,
                        "calibrated_score": decision.threat_score,
                        "is_anomaly": decision.band != ThreatBand.BENIGN,
                        "confidence": decision.threat_score,
                    }
                )
        if rows:
            session.execute(pg_insert(EventScore).values(rows))

    # ------------------------------------------------------------------
    # Stages 6-7 — CII and alerts
    # ------------------------------------------------------------------

    def _handle_anomalies(
        self,
        session: Any,
        scored: Sequence[ScoredFlow],
        inserted_ids: Sequence[Optional[int]],
        resolutions: list[tuple[Any, Any]],
        tripwire_fired: np.ndarray,
        is_anomaly: np.ndarray,
        meta: BatchMeta,
        fused_decisions: Optional[Sequence[FusedDecision]] = None,
    ) -> tuple["_CiiOutcome", "_AlertOutcome", "_IpsOutcome"]:
        """See the module docstring for the existing tripwire/volumetric
        policy. `fused_decisions`, when supplied, additionally widens
        this loop to flows the EXISTING channels did not flag
        (`is_anomaly[i]` is False) but whose fused decision independently
        cleared the alert band -- gated behind `hybrid_gates_alerts`
        (checked inside `_hybrid_alert_decision`, not here) and behind a
        cheap `fused.action == ALERT` pre-check so CII is never computed
        for the common case of an ordinary quiet flow (see P5-17: running
        Monte Carlo for a known-in-advance non-event is pure waste).
        Flows already covered by `is_anomaly[i]` are completely
        unaffected -- they take the exact branch this method always has.

        IPS (backend/ips/): computed for EVERY flow that reaches this far
        in the loop (both the `is_anomaly[i]` and `hybrid_candidate`
        branches), immediately after `cii_result` -- unlike the alert
        decision, which is branch-specific and may suppress/gate, the IPS
        decision is evaluated independent of whether an alert is actually
        created, per the target architecture (Risk + CII -> IPS Policy
        Engine -> Prevention Decision, not gated behind "and an alert
        exists"). It is a complete no-op whenever `ips_enabled` is False
        or `fused_decisions` is None (Hybrid IDS disabled) -- see
        `_compute_ips_decision`'s docstring for why the IPS layer
        structurally cannot run without the Hybrid IDS layer's output.
        """
        cii_outcome = _CiiOutcome()
        alert_outcome = _AlertOutcome()
        ips_outcome = _IpsOutcome()

        for i, scored_flow in enumerate(scored):
            hybrid_candidate = (
                fused_decisions is not None
                and not is_anomaly[i]
                and fused_decisions[i].action == ResponseAction.ALERT
            )
            if not is_anomaly[i] and not hybrid_candidate:
                continue
            event_id = inserted_ids[i]
            if event_id is None:
                continue  # already ingested; already has its CII/alert

            origin_asset = resolutions[i][0].asset_name
            is_tripwire = bool(tripwire_fired[i])

            # CII is computed for the anomaly itself, NOT gated behind the
            # alert decision. A blast radius is an analytical record in its
            # own right (backend/models.py says as much: a CiiSnapshot
            # survives its trigger event's deletion), and gating it on
            # alerting would mean suppressing the noisy volumetric channel
            # also silently discards every blast radius the demo's graph
            # view is meant to render.
            snapshot_id, cii_result = self._cii_for(
                session, origin_asset, scored_flow, event_id, cii_outcome
            )

            if self._ips_enabled and fused_decisions is not None:
                ips_decision = self._compute_ips_decision(
                    fused_decisions[i],
                    origin_asset,
                    resolutions[i][0].criticality,
                    cii_result,
                )
                if ips_decision is not None:
                    self._apply_ips_action(session, ips_decision, event_id, meta, ips_outcome)

            if is_anomaly[i]:
                # Broadcast the cii envelope BEFORE the alert-suppression
                # check, not after -- this channel already genuinely fired.
                # Previously this envelope was appended only alongside a
                # created alert, so a debounced repeat compromise (same
                # asset, inside alert_asset_debounce_sec) computed a fresh
                # blast radius -- a real Monte Carlo re-run against the
                # current graph state -- but never pushed it to the live
                # view: the graph sat showing the FIRST hit's cascade while
                # every later hit silently updated only Postgres. An
                # operator watching a sustained compromise would see the
                # CII overlay freeze the moment the debounce window opened,
                # which reads as "the blast radius stopped growing" when it
                # did not. Mirrors the comment above (computation was never
                # gated on alerting) now honoured for the broadcast too.
                self._publish_cii_envelope(cii_outcome, cii_result, snapshot_id, origin_asset, event_id)

                should_alert, suppressed_reason = self._alert_decision(
                    scored_flow, is_tripwire, origin_asset
                )
                if not should_alert:
                    alert_outcome.suppressed += 1
                    logger.debug(
                        "ingest: alert suppressed for %s (%s)",
                        origin_asset,
                        suppressed_reason,
                    )
                    continue
                alert = self._create_alert(
                    session, scored_flow, origin_asset, is_tripwire, snapshot_id
                )
            else:
                # hybrid_candidate is True here by the loop guard above.
                # UNLIKE the is_anomaly[i] branch above, the cii envelope
                # here is broadcast only once _hybrid_alert_decision
                # actually approves an alert (below), not unconditionally.
                # This is deliberate, not an inconsistency: an
                # is_anomaly[i] flow already came from a channel (tripwire
                # or volumetric) that genuinely fired -- suppression there
                # is purely a "don't page anyone" policy choice, so the
                # cascade is real and worth showing regardless. A
                # hybrid_candidate flow, by contrast, is one NEITHER
                # existing channel flagged; its only signal is the hybrid
                # layer's own fused opinion, which is explicitly
                # observable-not-authoritative while hybrid_gates_alerts
                # defaults False (see that setting's docstring). Lighting
                # up the graph's cascade overlay for a flow with no
                # corresponding alert to explain it would contradict that
                # posture -- an operator would see "CII cascade from X"
                # with nothing in the alerts panel accounting for it.
                should_alert, hybrid_reason = self._hybrid_alert_decision(
                    fused_decisions[i], origin_asset
                )
                if not should_alert:
                    logger.debug(
                        "ingest: hybrid alert not created for %s (%s)",
                        origin_asset,
                        hybrid_reason,
                    )
                    continue
                alert = self._create_hybrid_alert(
                    session, fused_decisions[i], origin_asset, snapshot_id
                )
                alert_outcome.hybrid_gated += 1
                self._publish_cii_envelope(cii_outcome, cii_result, snapshot_id, origin_asset, event_id)

            alert_outcome.created += 1
            alert_outcome.envelopes.append(
                {
                    "type": ENVELOPE_ALERT,
                    "data": {
                        "id": alert.id,
                        "ts": alert.ts.isoformat(),
                        "severity": alert.severity,
                        "asset": alert.asset,
                        "title": alert.title,
                        "detail": alert.detail,
                        "explanation": alert.explanation,
                        "cii_snapshot_id": snapshot_id,
                        "acknowledged": False,
                    },
                }
            )
        return cii_outcome, alert_outcome, ips_outcome

    # ------------------------------------------------------------------
    # IPS (backend/ips/) -- decision, enforcement, persistence, expiry
    # ------------------------------------------------------------------

    def _compute_ips_decision(
        self,
        fused: FusedDecision,
        origin_asset: str,
        asset_criticality: float,
        cii_result: Optional[CIIResult],
    ) -> Optional[PreventionDecision]:
        """Ask the policy engine what to do about `origin_asset`, or
        `None` if there is nothing to persist (`PreventionAction.OBSERVE`
        -- see `PreventionDecision`'s docstring for why OBSERVE decisions
        are not audit-worthy in the same way ALERT/RATE_LIMIT/BLOCK/
        QUARANTINE are, mirroring how a below-threshold volumetric score
        never gets its own `alerts` row either).

        `IPSPolicyEngine` itself never sees a raw flow or re-runs
        detection -- only the already-fused `FusedDecision` plus the two
        numbers this method already has to hand, per the requirement's
        "Consume Hybrid IDS outputs ... rather than implementing
        independent attack detection."
        """
        decision = self._policy_engine.decide(
            fused,
            target_asset=origin_asset,
            asset_criticality=asset_criticality,
            cii_median=(float(cii_result.cii_median) if cii_result is not None else None),
        )
        if decision.action == PreventionAction.OBSERVE:
            return None
        return decision

    def _apply_ips_action(
        self,
        session: Any,
        decision: PreventionDecision,
        event_id: int,
        meta: BatchMeta,
        outcome: "_IpsOutcome",
    ) -> None:
        """Apply one `PreventionDecision`: duplicate/conflict check against
        the active-mitigation registry, enforcement (fail-open), audit
        persistence, and envelope collection.

        Duplicate/conflicting-action protection (the requirement's own
        phrase): if `origin_asset` already has an active action at the
        same or higher severity (`PREVENTION_SEVERITY`), this decision is
        a no-op -- counted, never persisted, never re-enforced. A
        strictly higher-severity decision supersedes the existing one
        (its DB row is marked SUPERSEDED, not deleted -- the audit trail
        keeps every decision that was ever approved) before the new one
        is applied. This check runs even for a plain ALERT decision
        (severity 1): a corroborated-but-not-yet-block-worthy signal
        against an asset already under RATE_LIMIT should not re-persist
        a redundant ALERT row every batch.
        """
        existing = self._active_ips_actions.get(decision.target_asset)
        new_severity = PREVENTION_SEVERITY[decision.action]
        if existing is not None and PREVENTION_SEVERITY[existing.action] >= new_severity:
            outcome.duplicate_suppressed += 1
            return

        superseded_id = None
        if existing is not None:
            # Escalation: the existing (lower-severity) action is
            # superseded, not silently overwritten -- its row stays in
            # the audit trail with a terminal status.
            superseded_id = existing.action_id
            outcome.escalated += 1

        dry_run = self._ips_dry_run or not decision.is_active_prevention
        try:
            result = self._enforcement_adapter.apply(decision, dry_run=dry_run)
        except Exception:
            # Fail-open (the requirement's "graceful/fail-safe handling
            # when enforcement fails"): an adapter bug must never abort
            # the batch or block ordinary ingest -- unlike a persistence
            # failure (see the module docstring, "Failure semantics"),
            # a failed ENFORCEMENT attempt is recorded and swallowed, not
            # raised. `SimulatedEnforcementAdapter.apply()` itself cannot
            # raise (see its docstring), so reaching this branch means a
            # different adapter was injected and it misbehaved.
            logger.error(
                "ips: enforcement adapter raised for %s -> %s; failing open",
                decision.target_asset,
                decision.action.value,
                exc_info=True,
            )
            result_status = ActionStatus.FAILED
            result_detail = "enforcement adapter raised an exception; action not applied"
            outcome.failed += 1
        else:
            result_status = result.status
            result_detail = result.detail
            if result_status == ActionStatus.ENFORCED:
                outcome.enforced += 1
            elif result_status == ActionStatus.SIMULATED:
                outcome.simulated += 1
            else:
                outcome.failed += 1

        now_wall = meta.emitted_at
        expires_at = (
            now_wall + timedelta(seconds=decision.ttl_sec) if decision.ttl_sec else None
        )
        row = IpsAction(
            ts=now_wall,
            target_asset=decision.target_asset,
            action=decision.action.value,
            status=result_status.value,
            reason=f"{decision.reason} [{result_detail}]",
            evidence=_jsonable(dict(decision.evidence)),
            confidence=decision.confidence,
            dry_run=dry_run,
            triggering_event_id=event_id,
            replay_session_id=meta.replay_session_id,
            expires_at=expires_at,
        )
        session.add(row)
        session.flush()  # need row.id for the broadcast envelope + registry

        if superseded_id is not None:
            superseded_row = session.get(IpsAction, superseded_id)
            if superseded_row is not None:
                superseded_row.status = ActionStatus.SUPERSEDED.value
                superseded_row.rolled_back_at = now_wall
                superseded_row.rollback_reason = f"superseded by action {row.id}"

        # Active-mitigation registry: only ACTIVE prevention (RATE_LIMIT/
        # BLOCK/QUARANTINE) is tracked here -- a plain ALERT decision is
        # persisted (above) for the audit trail but never occupies a
        # registry slot, so it can never block or need to be superseded
        # by a later real prevention action against the same asset.
        if decision.is_active_prevention:
            self._active_ips_actions[decision.target_asset] = _IpsActionState(
                action=decision.action,
                action_id=row.id,
                expires_at=(self._clock() + decision.ttl_sec if decision.ttl_sec else None),
            )
            self._active_ips_actions.move_to_end(decision.target_asset)
            while len(self._active_ips_actions) > self._ips_active_action_cache_max_entries:
                self._active_ips_actions.popitem(last=False)

        outcome.decisions += 1
        outcome.envelopes.append(_ips_action_envelope(row))

    def _maybe_expire_ips_actions(self, session: Any) -> list[dict[str, Any]]:
        """Sweep the active-mitigation registry for TTL-expired entries,
        mark their DB rows EXPIRED, roll them back via the enforcement
        adapter, and return the `ips_action` envelopes to broadcast.

        Called once per batch (cheap: iterates an in-memory OrderedDict,
        not a DB scan -- the registry IS the live set of candidates,
        mirroring how `_debounce_ok` never queries the database either).
        A row that expires is looked up by id and updated in place, same
        pattern `_apply_ips_action` already uses for a superseded row.
        """
        now = self._clock()
        expired_assets = [
            asset
            for asset, state in self._active_ips_actions.items()
            if state.expires_at is not None and state.expires_at <= now
        ]
        if not expired_assets:
            return []

        envelopes: list[dict[str, Any]] = []
        for asset in expired_assets:
            state = self._active_ips_actions.pop(asset)
            self._enforcement_adapter.rollback(asset, state.action)
            if state.action_id is None:
                continue
            row = session.get(IpsAction, state.action_id)
            if row is None:
                continue
            row.status = ActionStatus.EXPIRED.value
            row.rolled_back_at = datetime.now(timezone.utc)
            row.rollback_reason = "TTL expired"
            envelopes.append(_ips_action_envelope(row))
        with self._lock:
            self._stats.ips_actions_expired += len(envelopes)
        return envelopes

    def rollback_ips_action(
        self, action_id: int, reason: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Manual operator rollback (`POST /api/ips/actions/{id}/rollback`,
        backend/routes.py) — the requirement's "unblock/rollback" control.

        Returns the `ips_action` envelope for the caller to broadcast (the
        route publishes it via the same `Broadcaster` this pipeline
        already holds — see `backend/routes.py`), or `None` if `action_id`
        does not exist (the route turns that into a 404) or is not a
        currently-active ACTIVE-PREVENTION action — either already
        terminal (rolled back / expired / superseded) or an `alert`-tier
        decision that was never enforced in the first place, so there is
        nothing to unblock (the route turns both into a 409, since
        re-rolling-back an already-inactive action is not a legitimate
        no-op the way stopping an already-stopped replay is: an operator
        asking to unblock something that is not currently blocking
        anything is telling us our active-mitigation state disagrees
        with theirs, which is worth surfacing, not silently swallowing).

        Public (unlike `_apply_ips_action`/`_maybe_expire_ips_actions`)
        because it is the one IPS operation genuinely triggered from
        OUTSIDE the ingest batch loop — an HTTP route, not a replay
        batch — so it opens its own short-lived session rather than
        requiring a caller-supplied one.
        """
        with self._session_factory() as session:
            row = session.get(IpsAction, action_id)
            if row is None:
                return None
            if row.status not in (ActionStatus.SIMULATED.value, ActionStatus.ENFORCED.value):
                return None
            if PreventionAction(row.action) not in ACTIVE_PREVENTION_ACTIONS:
                return None
            row.status = ActionStatus.ROLLED_BACK.value
            row.rolled_back_at = datetime.now(timezone.utc)
            row.rollback_reason = reason or "manual operator rollback"
            action = PreventionAction(row.action)
            target_asset = row.target_asset  # read while still attached -- see note below
            envelope = _ips_action_envelope(row)
        # `row` is DETACHED once the `with` block above exits (session_scope
        # commits and closes) -- every attribute access below must go
        # through `target_asset`/`action`/`envelope`, captured above,
        # never `row.*` again.

        self._enforcement_adapter.rollback(target_asset, action)
        existing = self._active_ips_actions.get(target_asset)
        if existing is not None and existing.action_id == action_id:
            del self._active_ips_actions[target_asset]
        return envelope

    def active_ips_actions(self) -> dict[str, str]:
        """Snapshot of the active-mitigation registry — `{target_asset:
        action}` for every asset currently under RATE_LIMIT/BLOCK/
        QUARANTINE. Read by `GET /api/ips/actions?active=true`
        (backend/routes.py) as a cheap in-memory cross-check against the
        DB query it also runs, and by tests."""
        with self._lock:
            return {asset: state.action.value for asset, state in self._active_ips_actions.items()}

    @staticmethod
    def _publish_cii_envelope(
        cii_outcome: "_CiiOutcome",
        cii_result: Optional[CIIResult],
        snapshot_id: Optional[int],
        origin_asset: str,
        event_id: int,
    ) -> None:
        """Append a `cii` WebSocket envelope for an already-computed
        snapshot, if there's one to publish.

        Split out purely to avoid writing this dict literal twice in
        `_handle_anomalies` (its two call sites publish at different
        points relative to alert creation -- see the comments at each
        call site for why that timing differs). No side effect on the
        database; `cii_outcome`'s DB persistence already happened inside
        `_cii_for()`, before either call site runs.
        """
        if cii_result is None or snapshot_id is None:
            return
        cii_outcome.envelopes.append(
            {
                "type": ENVELOPE_CII,
                "data": {
                    "snapshot_id": snapshot_id,
                    "origin_asset": origin_asset,
                    "cii_median": float(cii_result.cii_median),
                    "cii_p5": float(cii_result.cii_p5),
                    "cii_p95": float(cii_result.cii_p95),
                    "impacted": _jsonable(list(cii_result.impacted_assets)),
                    "trigger_event_id": event_id,
                },
            }
        )

    def _alert_decision(
        self, scored_flow: ScoredFlow, is_tripwire: bool, origin_asset: str
    ) -> tuple[bool, str]:
        """Apply the alert policy. See the module docstring.

        Returns `(should_alert, reason_if_not)`.
        """
        if not is_tripwire:
            if not self._alert_on_volumetric:
                return False, "volumetric-only channel disabled (precision ~0.02)"
            if scored_flow.calibrated_score < self._alert_volumetric_min_calibrated_score:
                return (
                    False,
                    f"calibrated_score {scored_flow.calibrated_score:.3f} < floor "
                    f"{self._alert_volumetric_min_calibrated_score:.3f}",
                )

        # Per-asset de-duplication, applied to EVERY channel including
        # tripwire: a honeytoken touched 400 times in one burst is one
        # incident, not 400 alerts.
        ok, reason = self._debounce_ok(origin_asset)
        if not ok:
            return False, reason
        return True, ""

    def _debounce_ok(self, origin_asset: str) -> tuple[bool, str]:
        """Shared per-asset alert debounce, extracted out of
        `_alert_decision` so `_hybrid_alert_decision` can apply the
        EXACT SAME cooldown state rather than a second independent
        OrderedDict -- both existing-channel alerts and hybrid-gated
        alerts on the same asset share one `_last_alert_at` clock, so a
        volumetric alert debounced this cycle cannot be immediately
        followed by a hybrid alert for the same asset two seconds later.

        Records a touch (mutates `_last_alert_at`) only when returning
        `(True, "")` -- i.e. only when the caller is actually about to
        create an alert, matching `_alert_decision`'s original behaviour
        of recording the touch as part of the same check that approves
        it.
        """
        if self._alert_asset_debounce_sec <= 0:
            return True, ""
        now = self._clock()
        last = self._last_alert_at.get(origin_asset)
        if last is not None and (now - last) < self._alert_asset_debounce_sec:
            return (
                False,
                f"asset debounce ({now - last:.1f}s < "
                f"{self._alert_asset_debounce_sec:.1f}s)",
            )
        self._last_alert_at[origin_asset] = now
        self._last_alert_at.move_to_end(origin_asset)
        while len(self._last_alert_at) > self._cii_cache_max_entries:
            self._last_alert_at.popitem(last=False)
        return True, ""

    def _cii_for(
        self,
        session: Any,
        origin_asset: str,
        scored_flow: ScoredFlow,
        event_id: int,
        outcome: "_CiiOutcome",
    ) -> tuple[Optional[int], Optional[CIIResult]]:
        """Compute (or reuse) the blast radius for `origin_asset`.

        Debounced per origin asset by `cii_debounce_sec`. Within the
        window the cached `CIIResult` and its snapshot id are BOTH
        reused (the docstring here used to describe this correctly while
        the code beneath it returned `None` for the result on a cache
        hit -- a real doc/code mismatch, fixed alongside the CII
        broadcast-on-suppression fix, since both bugs meant the same
        thing in practice: a live compromise updating the graph less
        often than it was actually being detected). Only the Monte Carlo
        recomputation is skipped, never the linkage or the result
        itself -- skipping the linkage would leave alerts with no blast
        radius, which is the one thing the alerts panel exists to show,
        and skipping the result silently starved
        `_publish_cii_envelope` on every cache-hit batch, which is
        MOST batches during a sustained attack once `cii_debounce_sec`
        (default 30s) is comfortably longer than the replay's batch
        interval.
        """
        # An asset that is not a node in the dependency graph has no edges
        # to propagate along, so compute_cascading_impact_full() returns an
        # all-zero CIIResult by construction — running the Monte Carlo to
        # obtain zeros is pure waste. This matters at real scale, not in
        # theory: AssetRegistry auto-registers one Unresolved_<ip> node per
        # unique unresolved IP (risk T5) and real CIC-IDS2017 carries
        # thousands, so without this guard a replay would run thousands of
        # full Monte Carlo simulations whose results are known in advance.
        # `_criticality_map` is keyed by exactly the graph's node set
        # (build_criticality_map -> compute_seed_rows -> build_graph), so
        # membership in it IS graph membership.
        if origin_asset not in self._criticality_map:
            outcome.skipped_not_in_graph += 1
            return None, None

        now = self._clock()
        entry = self._cii_cache.get(origin_asset)
        if entry is not None and (now - entry.computed_at) < self._cii_debounce_sec:
            self._cii_cache.move_to_end(origin_asset)
            outcome.reused += 1
            return entry.snapshot_id, entry.result

        result = compute_cascading_impact_full(
            anomalous_asset_name=origin_asset,
            anomaly_score=float(scored_flow.calibrated_score),
            criticality_map=self._criticality_map,
        )
        snapshot = CiiSnapshot(
            ts=datetime.now(timezone.utc),
            origin_asset=origin_asset,
            cii_median=float(result.cii_median),
            cii_p5=float(result.cii_p5),
            cii_p95=float(result.cii_p95),
            # The model types `impacted` as a JSON object, so the ordered
            # asset list is wrapped rather than stored as a bare array.
            impacted=_jsonable(
                {
                    "assets": list(result.impacted_assets),
                    "count": len(result.impacted_assets),
                }
            ),
            hop_details=_jsonable(dict(result.hop_details)),
            trigger_event_id=event_id,
        )
        session.add(snapshot)
        # Needed now, not at commit: the alert row about to be created
        # carries this snapshot's id as an FK, and the broadcast envelope
        # reports it.
        session.flush()

        self._cii_cache[origin_asset] = _CacheEntry(
            result=result, snapshot_id=snapshot.id, computed_at=now
        )
        self._cii_cache.move_to_end(origin_asset)
        # Bounded: AssetRegistry auto-registers one asset per unresolved
        # IP (risk T5), and real CIC-IDS2017 has thousands of them, so an
        # unbounded cache is an unbounded leak over a long replay.
        while len(self._cii_cache) > self._cii_cache_max_entries:
            self._cii_cache.popitem(last=False)

        outcome.computed += 1
        return snapshot.id, result

    def _create_alert(
        self,
        session: Any,
        scored_flow: ScoredFlow,
        origin_asset: str,
        is_tripwire: bool,
        snapshot_id: Optional[int],
    ) -> Alert:
        explanation = self._scorer.explain(scored_flow)
        if is_tripwire:
            severity = SEVERITY_CRITICAL
            title = TITLE_TRIPWIRE
            detail = (
                f"A honeytoken credential was used in traffic involving "
                f"{origin_asset}. Honeytokens have no legitimate use "
                f"anywhere in the system, so this is unambiguous "
                f"compromise, not a statistical inference."
            )
        else:
            severity = SEVERITY_WARNING
            title = TITLE_VOLUMETRIC
            top = explanation.get("top_feature")
            detail = (
                f"Volumetric anomaly on {origin_asset} "
                f"(calibrated score {scored_flow.calibrated_score:.3f}"
                + (f", driven by {top}" if top else "")
                + "). Unsupervised channel — see docs/DETECTION_STUDY.md "
                "for its measured precision."
            )
        alert = Alert(
            ts=datetime.now(timezone.utc),
            severity=severity,
            asset=origin_asset,
            title=title,
            detail=detail,
            explanation=_jsonable(explanation),
            cii_snapshot_id=snapshot_id,
            acknowledged=False,
        )
        session.add(alert)
        session.flush()  # need alert.id for the broadcast envelope
        return alert

    def _create_hybrid_alert(
        self,
        session: Any,
        fused: FusedDecision,
        origin_asset: str,
        snapshot_id: Optional[int],
    ) -> Alert:
        """Alert record for a hybrid-layer-gated escalation (see
        `_hybrid_alert_decision`).

        Deliberately NOT a third branch inside `_create_alert`: that
        method's `is_tripwire=False` branch writes "Anomalous traffic
        volume ... calibrated score X", which would misrepresent an
        alert that may have nothing to do with the volumetric channel --
        e.g. signature + beaconing both firing while the volumetric
        channel stayed quiet. This method names the actual detectors
        that drove the decision instead, straight from `fused.rationale`
        and `fused.fired_detectors`.

        Severity is WARNING, never CRITICAL: `Certainty.CONFIRMED`
        signals (the tripwire) already alert through the existing path
        with `already_anomaly=True`, so `_hybrid_alert_decision` can only
        reach this method for a HEURISTIC-only fused decision -- by
        definition never a confirmed compromise, so it must never be
        dressed up as one severity-wise either.
        """
        alert = Alert(
            ts=datetime.now(timezone.utc),
            severity=SEVERITY_WARNING,
            asset=origin_asset,
            title=TITLE_HYBRID,
            detail=(
                f"Hybrid fusion escalated {origin_asset} to band "
                f"{fused.band.value} (threat_score {fused.threat_score:.3f}) "
                f"on evidence from: {', '.join(fused.fired_detectors) or 'none'}. "
                f"{fused.rationale} Neither the tripwire nor the volumetric "
                f"channel alone reached the alert threshold for this flow -- "
                f"this alert exists because their combination did."
            ),
            explanation=_jsonable(
                {
                    "threat_score": fused.threat_score,
                    "band": fused.band.value,
                    "action": fused.action.value,
                    "rationale": fused.rationale,
                    "verdicts": [
                        {
                            "detector": v.detector,
                            "fired": v.fired,
                            "calibrated_score": v.calibrated_score,
                            "reliability": v.reliability,
                            "certainty": v.certainty.value,
                            "evidence": _jsonable(dict(v.evidence)),
                        }
                        for v in fused.verdicts
                    ],
                }
            ),
            cii_snapshot_id=snapshot_id,
            acknowledged=False,
        )
        session.add(alert)
        session.flush()
        return alert

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def _maybe_prune(self, session: Any) -> int:
        """Enforce the events-table cap every N batches.

        Ticket #2 shipped `prune_events()` and explicitly deferred the
        periodic call to this ticket (see
        `BACKEND_SETTINGS.db_event_retention_max_rows`). Not run per batch:
        the prune costs a COUNT plus a DELETE, wasted work when one batch
        adds at most 500 rows against a 500,000-row cap.
        """
        with self._lock:
            batch_number = self._stats.batches + 1
        if batch_number % self._retention_check_every_n_batches != 0:
            return 0
        pruned = prune_events(session)
        if pruned:
            logger.info("ingest: retention pruned %d old events", pruned)
        return pruned

    # ------------------------------------------------------------------
    # Stage 8 — broadcast
    # ------------------------------------------------------------------

    def _broadcast_batch(
        self,
        scored: Sequence[ScoredFlow],
        inserted_ids: Sequence[Optional[int]],
        resolutions: list[tuple[Any, Any]],
        tripwire_fired: np.ndarray,
        is_anomaly: np.ndarray,
        confidence: np.ndarray,
        meta: BatchMeta,
        fused_decisions: Optional[Sequence[FusedDecision]] = None,
    ) -> None:
        """Publish one `event` envelope per newly-inserted flow.

        Deduplicated flows are skipped: they were already broadcast on
        their first delivery, and re-pushing them would make the live feed
        show a burst of repeats after any ingest retry.

        `fused_decisions`, when supplied, adds an ADDITIVE `hybrid` key
        to each envelope's `data` -- every key that existed before this
        layer is unchanged, so an older frontend build ignoring an
        unknown key keeps working exactly as it did. Deliberately a
        compact summary (`threat_score`/`band`/`action`/
        `fired_detectors`/`rationale`), not the full per-verdict evidence
        blob `_create_hybrid_alert` writes into an alert's `explanation`
        -- this envelope is broadcast for EVERY event at up to ~2000/s
        (measured, Ticket #10), so it stays small; the full breakdown is
        one click away via the alert it may have produced.
        """
        for i, scored_flow in enumerate(scored):
            event_id = inserted_ids[i]
            if event_id is None:
                continue
            flow = scored_flow.flow
            self._safe_publish(
                {
                    "type": ENVELOPE_EVENT,
                    "data": {
                        "id": event_id,
                        "ts": flow.ts.isoformat(),
                        "observed_at": meta.emitted_at.isoformat(),
                        "source_ip": flow.source_ip,
                        "destination_ip": flow.destination_ip,
                        "source_asset": resolutions[i][0].asset_name,
                        "destination_asset": resolutions[i][1].asset_name,
                        "protocol": flow.protocol,
                        "bytes": flow.bytes,
                        "packets": flow.packets,
                        "duration_sec": flow.duration_sec,
                        "raw_score": scored_flow.raw_score,
                        "calibrated_score": scored_flow.calibrated_score,
                        "is_anomaly": bool(is_anomaly[i]),
                        "tripwire_fired": bool(tripwire_fired[i]),
                        "confidence": float(confidence[i]),
                        "replay_session_id": str(meta.replay_session_id),
                        "batch_index": meta.batch_index,
                        # Ticket #19 (§A): the live feed must not render
                        # millisecond/second precision this capture day does
                        # not have. `flow.timing_provenance` is the per-event
                        # ground truth (`capture_seconds` vs
                        # `interpolated_minute_bucket`, backend/models.py) —
                        # the frontend uses it to decide how much of `ts` is
                        # honest to display, instead of a hardcoded format.
                        "timing_provenance": flow.timing_provenance,
                        # BATCH_ORIGIN_REPLAY | BATCH_ORIGIN_INJECTED.
                        # Additive, and load-bearing: Ticket #13 injects
                        # REAL captured attack flows re-targeted onto a
                        # curated asset as an operator "what-if", and that
                        # must never be mistaken for observed capture
                        # telemetry. `events.raw` already records it, so
                        # GET /api/events could distinguish the two — but
                        # the live feed is the surface an operator
                        # actually watches, and without this field a
                        # WebSocket client cannot tell an injected
                        # scenario from real traffic in real time.
                        "batch_origin": meta.origin,
                        "hybrid": (
                            {
                                "threat_score": fused_decisions[i].threat_score,
                                "band": fused_decisions[i].band.value,
                                "action": fused_decisions[i].action.value,
                                "fired_detectors": list(fused_decisions[i].fired_detectors),
                                "rationale": fused_decisions[i].rationale,
                            }
                            if fused_decisions is not None
                            else None
                        ),
                    },
                }
            )

    def _safe_publish(self, envelope: dict[str, Any]) -> None:
        """Publish, converting any transport failure into a counter.

        Broadcasting happens after the transaction commits, so a dead
        WebSocket must never roll back or re-drive data already durable in
        Postgres -- see the module docstring, "Failure semantics".
        """
        try:
            self._broadcaster.publish(envelope)
        except Exception:
            logger.warning(
                "ingest: broadcast failed for envelope type %r; "
                "data is already committed",
                envelope.get("type"),
                exc_info=True,
            )
            with self._lock:
                self._stats.broadcast_failures += 1

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def stats(self) -> IngestStats:
        """Snapshot of the cumulative counters. Ticket #16 reads this."""
        with self._lock:
            return IngestStats(**vars(self._stats))

    def cii_cache_size(self) -> int:
        return len(self._cii_cache)

    def publish_envelope(self, envelope: dict[str, Any]) -> None:
        """Publish one envelope through this pipeline's `Broadcaster`,
        outside the normal `ingest_batch` flow. The one real caller is
        `POST /api/ips/actions/{id}/rollback` (backend/routes.py): a
        manual rollback is a state change triggered from an HTTP request,
        not a replay batch, so there is no `ingest_batch` call underway
        to append the envelope to — this is the public seam that lets the
        route push it live anyway, going through the same `_safe_publish`
        failure-isolation every other envelope does (a dead WebSocket
        must not turn a successful, already-committed rollback into a
        500).
        """
        self._safe_publish(envelope)


@dataclass
class _CiiOutcome:
    computed: int = 0
    reused: int = 0
    skipped_not_in_graph: int = 0
    envelopes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _AlertOutcome:
    created: int = 0
    suppressed: int = 0
    #: Of `created`, how many exist ONLY because the hybrid layer's
    #: `_hybrid_alert_decision` widened the gate -- i.e. neither the
    #: tripwire nor the volumetric channel would have alerted on that
    #: flow by itself. Always 0 when hybrid_gates_alerts is False.
    hybrid_gated: int = 0
    envelopes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _IpsOutcome:
    """Per-batch IPS (backend/ips/) outcome — see `IngestStats`'s matching
    `ips_*` fields for what each counter means."""

    decisions: int = 0
    enforced: int = 0
    simulated: int = 0
    duplicate_suppressed: int = 0
    escalated: int = 0
    failed: int = 0
    envelopes: list[dict[str, Any]] = field(default_factory=list)
