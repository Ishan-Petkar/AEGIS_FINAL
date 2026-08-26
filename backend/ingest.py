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
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Sequence

import numpy as np
import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.config import BACKEND_SETTINGS
from backend.db import session_scope
from backend.models import Alert, CiiSnapshot, Event, EventScore
from backend.replay_engine import BatchMeta
from backend.replay_reader import ReplayFlow
from backend.retention import prune_events
from backend.seed import compute_seed_rows
from backend.streaming import ScoredFlow, StreamingScorer, fuse_tripwire_confidence
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

#: WebSocket envelope types (docs/PHASE5_BUILD_PLAN.md section 7). Ticket
#: #9 owns the transport; this module owns the payloads it carries.
ENVELOPE_EVENT = "event"
ENVELOPE_ALERT = "alert"
ENVELOPE_CII = "cii"

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

#: Titles are stable strings, not f-strings over asset names, so the
#: frontend can group/filter on them without parsing prose.
TITLE_TRIPWIRE = "Honeytoken credential used"
TITLE_VOLUMETRIC = "Anomalous traffic volume"


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


@dataclass
class _CacheEntry:
    """One debounce-cache slot: a CII result plus when it was computed."""

    result: CIIResult
    snapshot_id: Optional[int]
    computed_at: float


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
    """

    def __init__(
        self,
        scorer: StreamingScorer,
        broadcaster: Optional[Broadcaster] = None,
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
    ) -> None:
        if scorer is None:
            raise ValueError("IngestPipeline requires a fitted StreamingScorer")

        self._scorer = scorer
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
                session, scored, inserted_ids, tripwire_fired, is_anomaly, confidence
            )
            cii_outcomes, alert_outcomes = self._handle_anomalies(
                session, scored, inserted_ids, resolutions, tripwire_fired, is_anomaly
            )
            pruned = self._maybe_prune(session)

        # ---- 8. broadcast (AFTER commit) ------------------------------
        self._broadcast_batch(
            scored, inserted_ids, resolutions, tripwire_fired, is_anomaly, confidence, meta
        )
        for envelope in cii_outcomes.envelopes + alert_outcomes.envelopes:
            self._safe_publish(envelope)

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
    ) -> None:
        """Insert one volumetric score row per event, plus a tripwire row
        only where the tripwire actually fired.

        A tripwire row for every event would double `event_scores` volume
        to record "no honeytoken was touched", which is the default state
        of every ordinary flow and carries no information. Where it DID
        fire, the row is written so an operator can see the deception
        channel's verdict alongside the volumetric one.
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
    ) -> tuple["_CiiOutcome", "_AlertOutcome"]:
        cii_outcome = _CiiOutcome()
        alert_outcome = _AlertOutcome()

        for i, scored_flow in enumerate(scored):
            if not is_anomaly[i]:
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
            if cii_result is not None and snapshot_id is not None:
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
        return cii_outcome, alert_outcome

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
        if self._alert_asset_debounce_sec > 0:
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
        window the cached `CIIResult` and its snapshot id are reused, so
        the alert still carries a blast radius -- only the Monte Carlo
        recomputation is skipped, never the linkage. That distinction
        matters: skipping the linkage would leave alerts with no blast
        radius, which is the one thing the alerts panel exists to show.
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
            return entry.snapshot_id, None

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
    ) -> None:
        """Publish one `event` envelope per newly-inserted flow.

        Deduplicated flows are skipped: they were already broadcast on
        their first delivery, and re-pushing them would make the live feed
        show a burst of repeats after any ingest retry.
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
    envelopes: list[dict[str, Any]] = field(default_factory=list)
