"""
tests/test_ingest.py — Phase 5 Ticket #7: score -> persist -> broadcast,
CII debounce.

The default suite touches NO database (CI has no Postgres): it drives
`IngestPipeline` with a fake session that records what would have been
written, so alert policy, CII debounce, tripwire routing, dedup handling,
envelope shapes, and failure isolation are all exercised without Postgres.

Live-DB tests (real insert -> read back -> assert FK linkage and ordering)
are gated behind AEGIS_TEST_LIVE_DB=1 and skipped by default, matching
tests/test_backend_models.py.
"""

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from backend.config import BackendSettings
from backend.ingest import (
    DETECTOR_SUPERVISED,
    DETECTOR_TRIPWIRE,
    DETECTOR_VOLUMETRIC,
    ENVELOPE_ALERT,
    ENVELOPE_CII,
    ENVELOPE_EVENT,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    TITLE_TRIPWIRE,
    TITLE_VOLUMETRIC,
    CollectingBroadcaster,
    IngestPipeline,
    NullBroadcaster,
    build_criticality_map,
    compute_risk_index,
    default_tripwire_signal,
)
from backend.models import Alert, CiiSnapshot, IpsAction
from backend.replay_engine import BatchMeta
from backend.replay_reader import ReplayFlow
from backend.streaming import ScoredFlow
from backend.supervised_detector import SupervisedScoredFlow

BASE_TS = datetime(2017, 7, 7, 9, 0, 0, tzinfo=timezone.utc)

# A real node in the dependency graph, so the CII guard lets it through.
GRAPH_ASSET_IP = "10.0.1.20"  # City_Payment_Gateway


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def make_flow(row_id: str, *, src_ip: str = GRAPH_ASSET_IP, ts_offset: int = 0, **kw):
    defaults = dict(
        ts=BASE_TS + timedelta(seconds=ts_offset),
        source_ip=src_ip,
        source_port=443,
        destination_ip="10.0.1.21",
        destination_port=80,
        protocol="TCP",
        duration_sec=1.0,
        packets=10,
        bytes=1000,
        label="BENIGN",
        is_attack=False,
        timing_provenance="capture_seconds",
        source_row_id=row_id,
        source_dataset="cic_ids2017",
    )
    defaults.update(kw)
    return ReplayFlow(**defaults)


class FakeScorer:
    """Stands in for a fitted StreamingScorer.

    Returns whatever anomaly verdict the test asks for. Crucially it never
    fits anything, so these tests cannot accidentally pass because of a
    refit (that is Invariant B's own test's job, in
    tests/test_streaming_scorer.py).
    """

    def __init__(self, anomaly_flags=None, calibrated=0.99):
        self.anomaly_flags = anomaly_flags
        self.calibrated = calibrated
        self.score_batch_calls = 0
        self.score_event_calls = 0

    def score_batch(self, flows):
        self.score_batch_calls += 1
        out = []
        for i, f in enumerate(flows):
            if self.anomaly_flags is None:
                is_anom = False
            else:
                is_anom = self.anomaly_flags[i]
            out.append(
                ScoredFlow(
                    flow=f,
                    raw_score=-0.5 if is_anom else 0.1,
                    calibrated_score=self.calibrated if is_anom else 0.1,
                    is_anomaly=bool(is_anom),
                    z_scores=(1.0, 2.0, 3.0),
                )
            )
        return out

    def score_event(self, flow):
        self.score_event_calls += 1
        return self.score_batch([flow])[0]

    def explain(self, scored):
        return {
            "top_feature": "bytes",
            "features": [{"name": "bytes", "z": 47.0, "degenerate_baseline": False}],
        }


class FakeSupervisedScorer:
    """Stands in for a fitted SupervisedFlowScorer (Phase B improvement
    pass) — mirrors FakeScorer's pattern exactly, one level down (a
    smaller, independent fake rather than extending FakeScorer, since the
    two channels return different dataclasses and must never be
    conflated)."""

    def __init__(self, anomaly_flags=None, p_attack=0.97):
        self.anomaly_flags = anomaly_flags
        self.p_attack = p_attack
        self.score_batch_calls = 0

    def score_batch(self, flows):
        self.score_batch_calls += 1
        out = []
        for i, f in enumerate(flows):
            is_anom = False if self.anomaly_flags is None else self.anomaly_flags[i]
            p = self.p_attack if is_anom else 1.0 - self.p_attack
            out.append(
                SupervisedScoredFlow(
                    flow=f,
                    raw_score=-p,
                    calibrated_score=p,
                    is_anomaly=bool(is_anom),
                )
            )
        return out


def stmt_table_name(stmt) -> str:
    table = getattr(stmt, "table", None)
    return getattr(table, "name", "")


def rows_of(stmt) -> list[dict]:
    """Flatten an insert statement's multi-values payload to plain dicts.

    SQLAlchemy keys `_multi_values` rows by `Column` objects, not strings,
    so tests normalise to column names rather than asserting against the
    ORM's internal key type.
    """
    out = []
    for group in getattr(stmt, "_multi_values", ()) or ():
        for row in group:
            out.append({getattr(k, "name", str(k)): v for k, v in row.items()})
    return out


def stmts_for(session, table_name: str) -> list:
    return [s for s in session.executed if stmt_table_name(s) == table_name]


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    """Records ORM adds and answers the one bulk-insert execute() call.

    `next_event_ids` is the id sequence handed back as though Postgres had
    assigned them; setting it shorter than the batch simulates rows that
    ON CONFLICT DO NOTHING skipped (the D4 dedup path).
    """

    def __init__(self, next_event_ids=None):
        self.next_event_ids = list(next_event_ids) if next_event_ids is not None else None
        self.added = []
        self.executed = []
        self.flushes = 0
        self._next_pk = 1000

    def execute(self, stmt):
        self.executed.append(stmt)
        if stmt_table_name(stmt) == "events":
            return FakeResult(self._event_returning(stmt))
        return FakeResult([])

    def _event_returning(self, stmt):
        # Recover the source_row_id values in insert order from the
        # statement's multi-values payload, mimicking what RETURNING
        # yields under ON CONFLICT DO NOTHING (inserted rows only).
        row_ids = [r["source_row_id"] for r in rows_of(stmt)]
        if self.next_event_ids is None:
            ids = list(range(1, len(row_ids) + 1))
        else:
            ids = self.next_event_ids
        return [(ids[i], row_ids[i]) for i in range(min(len(ids), len(row_ids)))]

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_pk
                self._next_pk += 1

    def get(self, model, pk):
        """Minimal stand-in for `Session.get()` — used by
        `IngestPipeline._apply_ips_action`'s supersede path and
        `_maybe_expire_ips_actions` to look up a previously-added row by
        id. Real SQLAlchemy would hit the identity map or the DB; this
        fake only ever needs to find rows this same fake already added.
        """
        for obj in self.added:
            if isinstance(obj, model) and getattr(obj, "id", None) == pk:
                return obj
        return None

    def alerts(self):
        return [o for o in self.added if isinstance(o, Alert)]

    def snapshots(self):
        return [o for o in self.added if isinstance(o, CiiSnapshot)]

    def ips_actions(self):
        return [o for o in self.added if isinstance(o, IpsAction)]


def make_pipeline(scorer=None, session=None, broadcaster=None, **kw):
    session = session if session is not None else FakeSession()

    @contextmanager
    def factory():
        yield session

    pipeline = IngestPipeline(
        scorer=scorer or FakeScorer(),
        broadcaster=broadcaster if broadcaster is not None else CollectingBroadcaster(),
        session_factory=factory,
        **kw,
    )
    return pipeline, session


def make_meta(batch_index=1, session_id=None):
    return BatchMeta(
        replay_session_id=session_id or uuid.uuid4(),
        day="friday-morning",
        speed=20.0,
        batch_index=batch_index,
        emitted_at=datetime.now(timezone.utc),
        lag_seconds=0.0,
        origin="replay",
    )


# ---------------------------------------------------------------------------
# Consumer contract
# ---------------------------------------------------------------------------


def test_call_signature_matches_replay_consumer():
    """__call__(batch, meta) is what ReplayEngine hands its consumer."""
    pipeline, _ = make_pipeline()
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.flows_received == 1


def test_empty_batch_is_a_noop():
    pipeline, session = make_pipeline()
    result = pipeline([], make_meta())
    assert result.flows_received == 0
    assert result.events_inserted == 0
    assert session.executed == []
    assert pipeline.stats().batches == 1


def test_requires_a_scorer():
    with pytest.raises(ValueError, match="StreamingScorer"):
        IngestPipeline(scorer=None)


# ---------------------------------------------------------------------------
# Scoring — batched, never per-event
# ---------------------------------------------------------------------------


def test_scores_the_whole_batch_in_one_call():
    """P5-10/Ticket #5: score_event() in a loop is a performance bug
    (~366x worse per event). One score_batch() call per batch."""
    scorer = FakeScorer()
    pipeline, _ = make_pipeline(scorer=scorer)
    pipeline([make_flow(f"f:{i}") for i in range(50)], make_meta())
    assert scorer.score_batch_calls == 1
    assert scorer.score_event_calls == 0


# ---------------------------------------------------------------------------
# Tripwire + fusion (Invariant C — uses the real detector)
# ---------------------------------------------------------------------------


def test_default_tripwire_signal_is_false_for_plain_replay_flow():
    """Replayed 2017 capture traffic never touched an AEGIS honeytoken."""
    assert default_tripwire_signal(make_flow("f:1")) is False


# ---------------------------------------------------------------------------
# compute_risk_index — Ticket #16, decision D16-1
# ---------------------------------------------------------------------------


def test_compute_risk_index_no_unacknowledged_alerts_is_zero_not_none():
    """Zero unacknowledged alerts is a real state -- 0, never None/'—'."""
    cm = {"City_Payment_Gateway": 0.95}
    assert compute_risk_index([], cm) == 0


def test_compute_risk_index_critical_alert_on_criticality_one_asset():
    """A single critical alert on the single highest-criticality asset in
    the graph (criticality 1.0) reads ~20/100 under the default settings
    (risk_severity_weight_critical=1.0, risk_index_full_scale=5.0) --
    pinning the documented example in BackendSettings.risk_index_full_scale's
    docstring."""
    cm = {"Power_Substation": 1.0}
    index = compute_risk_index([(SEVERITY_CRITICAL, "Power_Substation", 1)], cm)
    assert index == 20


def test_compute_risk_index_ignores_asset_not_in_criticality_map():
    """An asset with no real criticality basis (e.g. an auto-registered
    Unresolved_<ip>) contributes nothing -- never a fabricated guess."""
    cm = {"City_Payment_Gateway": 0.95}
    index = compute_risk_index([(SEVERITY_CRITICAL, "Unresolved_10.9.9.9", 1)], cm)
    assert index == 0


def test_compute_risk_index_unknown_severity_uses_default_weight():
    cm = {"City_Payment_Gateway": 1.0}
    settings = BackendSettings(
        risk_severity_weight_critical=1.0,
        risk_severity_weight_warning=0.35,
        risk_severity_weight_default=0.1,
        risk_index_full_scale=1.0,
    )
    index = compute_risk_index(
        [("normal", "City_Payment_Gateway", 1)], cm, settings=settings
    )
    assert index == 10  # 100 * (0.1 * 1.0) / 1.0


def test_compute_risk_index_warning_weighted_below_critical():
    cm = {"City_Payment_Gateway": 1.0}
    settings = BackendSettings(risk_index_full_scale=1.0)
    critical = compute_risk_index(
        [(SEVERITY_CRITICAL, "City_Payment_Gateway", 1)], cm, settings=settings
    )
    warning = compute_risk_index(
        [(SEVERITY_WARNING, "City_Payment_Gateway", 1)], cm, settings=settings
    )
    assert warning < critical


def test_compute_risk_index_is_clamped_at_100():
    cm = {"Power_Substation": 1.0}
    settings = BackendSettings(risk_index_full_scale=1.0)
    index = compute_risk_index(
        [(SEVERITY_CRITICAL, "Power_Substation", 50)], cm, settings=settings
    )
    assert index == 100


def test_compute_risk_index_counts_multiply_contribution():
    """The (severity, asset, count) shape mirrors a GROUP BY aggregate --
    N alerts on the same asset/severity must contribute N times, not once."""
    cm = {"City_Payment_Gateway": 1.0}
    settings = BackendSettings(risk_index_full_scale=10.0)
    one = compute_risk_index(
        [(SEVERITY_CRITICAL, "City_Payment_Gateway", 1)], cm, settings=settings
    )
    five = compute_risk_index(
        [(SEVERITY_CRITICAL, "City_Payment_Gateway", 5)], cm, settings=settings
    )
    assert five == pytest.approx(one * 5, abs=1)


def test_compute_risk_index_acknowledging_the_only_alert_drops_index_to_zero():
    """The behaviour the ticket's own verification hinges on: removing an
    alert from the unacknowledged set (i.e. acking it) must visibly lower
    the index -- here, to zero, since it was the only outstanding alert."""
    cm = {"City_Payment_Gateway": 0.95}
    before = compute_risk_index([(SEVERITY_CRITICAL, "City_Payment_Gateway", 1)], cm)
    after = compute_risk_index([], cm)  # simulates the alert being acked
    assert before > 0
    assert after == 0
    assert after < before


def test_tripwire_signal_hook_drives_the_real_detector():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: f.source_row_id == "hot:1",
    )
    result = pipeline([make_flow("hot:1")], make_meta())
    assert result.tripwire_hits == 1
    # Tripwire fired even though the volumetric detector said normal —
    # that is the OR-fusion contract.
    assert result.anomalies == 1


def test_fusion_confidence_written_to_event_scores():
    """Unrelated to the Hybrid IDS layer -- specifically pins
    fuse_tripwire_confidence's own confidence_both constant, which the
    hybrid row's fused threat_score (a different scale entirely) would
    not share, so hybrid_enabled=False keeps this test's loop assertion
    ("every row shares this one confidence value") meaningful."""
    from settings import SETTINGS

    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True]),
        tripwire_signal=lambda f: True,
        hybrid_enabled=False,
    )
    pipeline([make_flow("f:1")], make_meta())
    score_stmt = stmts_for(session, "event_scores")
    assert len(score_stmt) == 1
    rows = rows_of(score_stmt[0])
    detectors = {r["detector"] for r in rows}
    assert detectors == {DETECTOR_VOLUMETRIC, DETECTOR_TRIPWIRE}
    for r in rows:
        assert r["confidence"] == pytest.approx(SETTINGS.deception.confidence_both)


def test_tripwire_score_row_only_written_when_it_fires():
    """A tripwire row per event would double event_scores volume to record
    'no honeytoken was touched' — the default state of every flow.
    hybrid_enabled=False: this test is about the pre-existing tripwire
    row-economy invariant, not the hybrid layer's own (separately tested)
    row."""
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False, False]), hybrid_enabled=False
    )
    pipeline([make_flow("f:1"), make_flow("f:2")], make_meta())
    rows = rows_of(stmts_for(session, "event_scores")[0])
    assert all(r["detector"] == DETECTOR_VOLUMETRIC for r in rows)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Supervised (KNOWN-THREAT) channel — Phase B improvement pass. Purely
# additive: must never change is_anomaly, the alert policy, or the
# volumetric/tripwire rows' own confidence — see IngestPipeline's
# supervised_scorer docstring.
# ---------------------------------------------------------------------------


def test_no_supervised_row_when_channel_not_configured():
    """Default posture (no artifact built / not passed to IngestPipeline):
    behaviour is byte-for-byte what it was before this channel existed."""
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True]), hybrid_enabled=False
    )
    pipeline([make_flow("f:1")], make_meta())
    rows = rows_of(stmts_for(session, "event_scores")[0])
    assert DETECTOR_SUPERVISED not in {r["detector"] for r in rows}
    assert len(rows) == 1  # volumetric only (hybrid_enabled=False: this test
    # is about the supervised-channel row being conditional, not the
    # hybrid layer's own separately-tested row)


def test_supervised_row_written_when_channel_configured():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        supervised_scorer=FakeSupervisedScorer(anomaly_flags=[True], p_attack=0.93),
    )
    pipeline([make_flow("f:1")], make_meta())
    rows = rows_of(stmts_for(session, "event_scores")[0])
    detectors = {r["detector"] for r in rows}
    assert DETECTOR_SUPERVISED in detectors
    sup_row = next(r for r in rows if r["detector"] == DETECTOR_SUPERVISED)
    assert sup_row["is_anomaly"] is True
    assert sup_row["calibrated_score"] == pytest.approx(0.93)
    assert sup_row["raw_score"] == pytest.approx(-0.93)
    # This channel's own confidence is its own P(attack) — NOT the fused
    # volumetric/tripwire confidence (which is 0.0 here: neither fired).
    assert sup_row["confidence"] == pytest.approx(0.93)


def test_supervised_channel_never_changes_is_anomaly_or_alert_policy():
    """The load-bearing invariant: wiring this channel must be provably
    unable to move any already-published statistic (alert counts,
    suppression counts, is_anomaly) — it is purely informational."""
    common_kwargs = dict(scorer=FakeScorer(anomaly_flags=[False]))  # volumetric says normal
    pipeline_without, session_without = make_pipeline(**common_kwargs)
    pipeline_with, session_with = make_pipeline(
        **common_kwargs, supervised_scorer=FakeSupervisedScorer(anomaly_flags=[True], p_attack=0.99)
    )

    result_without = pipeline_without([make_flow("f:1")], make_meta())
    result_with = pipeline_with([make_flow("f:2")], make_meta())

    # Even though the FAKE supervised scorer screams "attack, 0.99
    # confidence", is_anomaly/alerts/anomalies are identical to the run
    # with no supervised channel at all.
    assert result_with.anomalies == result_without.anomalies == 0
    assert result_with.alerts_created == result_without.alerts_created
    assert result_with.alerts_suppressed == result_without.alerts_suppressed

    volumetric_row_with = next(
        r for r in rows_of(stmts_for(session_with, "event_scores")[0]) if r["detector"] == DETECTOR_VOLUMETRIC
    )
    volumetric_row_without = next(
        r for r in rows_of(stmts_for(session_without, "event_scores")[0]) if r["detector"] == DETECTOR_VOLUMETRIC
    )
    # The volumetric row's own confidence is unaffected by the supervised
    # channel's presence.
    assert volumetric_row_with["confidence"] == volumetric_row_without["confidence"]


def test_supervised_scorer_called_once_per_batch_never_per_event():
    supervised = FakeSupervisedScorer(anomaly_flags=[False, True, False])
    pipeline, _ = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False, False, False]),
        supervised_scorer=supervised,
    )
    pipeline([make_flow("f:1"), make_flow("f:2"), make_flow("f:3")], make_meta())
    assert supervised.score_batch_calls == 1


def test_supervised_row_respects_deduplication():
    """A deduplicated event (inserted_ids[i] is None) must not get a
    supervised row either — mirrors the volumetric/tripwire rows' own
    dedup handling."""
    session = FakeSession(next_event_ids=[1000])  # only the first of 2 flows is "new"
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False, False]),
        session=session,
        supervised_scorer=FakeSupervisedScorer(anomaly_flags=[False, False]),
    )
    pipeline([make_flow("f:1"), make_flow("f:2")], make_meta())
    rows = rows_of(stmts_for(session, "event_scores")[0])
    supervised_rows = [r for r in rows if r["detector"] == DETECTOR_SUPERVISED]
    assert len(supervised_rows) == 1
    assert supervised_rows[0]["event_id"] == 1000


# ---------------------------------------------------------------------------
# Alert policy — the load-bearing decision (docs/DETECTION_STUDY.md)
# ---------------------------------------------------------------------------


def test_volumetric_only_anomaly_raises_no_alert_by_default():
    """Measured precision ~0.02 (5 TP / 811 FP). Alerting on this channel
    fills the panel with ~800 junk rows per replay day."""
    pipeline, session = make_pipeline(scorer=FakeScorer(anomaly_flags=[True]))
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.anomalies == 1
    assert result.alerts_created == 0
    assert result.alerts_suppressed == 1
    assert session.alerts() == []


def test_tripwire_anomaly_always_alerts():
    """A honeytoken has zero legitimate use, so it cannot false-positive."""
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: True,
    )
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 1
    alert = session.alerts()[0]
    assert alert.severity == SEVERITY_CRITICAL
    assert alert.title == TITLE_TRIPWIRE


def test_volumetric_alert_enabled_still_requires_the_score_floor():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True], calibrated=0.5),
        alert_on_volumetric=True,
        alert_volumetric_min_calibrated_score=0.9,
    )
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 0
    assert result.alerts_suppressed == 1


def test_volumetric_alert_fires_above_the_floor():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True], calibrated=0.95),
        alert_on_volumetric=True,
        alert_volumetric_min_calibrated_score=0.9,
    )
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 1
    assert session.alerts()[0].severity == SEVERITY_WARNING
    assert session.alerts()[0].title == TITLE_VOLUMETRIC


def test_suppression_does_not_hide_the_event_or_its_score():
    """Suppression is alerting-only. The anomaly is still scored,
    persisted, and broadcast — it just does not page an operator."""
    broadcaster = CollectingBroadcaster()
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True]), broadcaster=broadcaster
    )
    pipeline([make_flow("f:1")], make_meta())
    events = broadcaster.of_type(ENVELOPE_EVENT)
    assert len(events) == 1
    assert events[0]["data"]["is_anomaly"] is True
    assert len(stmts_for(session, "event_scores")) == 1


def test_alerts_deduplicated_per_asset_by_debounce():
    """A honeytoken touched 400 times in a burst is one incident."""
    clock = {"t": 0.0}
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False] * 5),
        tripwire_signal=lambda f: True,
        alert_asset_debounce_sec=60.0,
        clock=lambda: clock["t"],
    )
    result = pipeline([make_flow(f"f:{i}") for i in range(5)], make_meta())
    assert result.alerts_created == 1
    assert result.alerts_suppressed == 4


def test_alert_debounce_expires():
    clock = {"t": 0.0}
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: True,
        alert_asset_debounce_sec=60.0,
        clock=lambda: clock["t"],
    )
    pipeline([make_flow("f:1")], make_meta(1))
    clock["t"] = 61.0
    result = pipeline([make_flow("f:2")], make_meta(2))
    assert result.alerts_created == 1


def test_cii_envelope_still_broadcast_when_alert_is_debounce_suppressed():
    """Regression guard: a debounced repeat compromise on the same asset
    must not freeze the live graph's cascade view. Before this fix, the
    cii envelope was only appended alongside a CREATED alert -- a second
    tripwire hit inside alert_asset_debounce_sec computed (or reused) a
    real blast radius but never pushed it to the WebSocket, so an
    operator watching a sustained compromise would see the cascade
    overlay stop updating after the first hit even though the
    compromise, and its recorded blast radius, was ongoing."""
    clock = {"t": 0.0}
    broadcaster = CollectingBroadcaster()
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: True,
        alert_asset_debounce_sec=60.0,
        clock=lambda: clock["t"],
        broadcaster=broadcaster,
    )
    first = pipeline([make_flow("f:1")], make_meta(1))
    clock["t"] = 5.0  # well inside the 60s debounce window
    second = pipeline([make_flow("f:2")], make_meta(2))

    assert first.alerts_created == 1
    assert second.alerts_created == 0
    assert second.alerts_suppressed == 1  # confirms this really was suppressed, not skipped
    # The second batch is a genuine CII cache HIT too (cii_debounce_sec's
    # own default, 30s, also hasn't elapsed) -- proving the fix covers
    # both debounce paths, not just the alert one. See the companion fix
    # in _cii_for: a cache hit used to return cii_result=None, which
    # would have silently starved this exact envelope regardless of the
    # alert-suppression fix above.
    assert second.cii_reused == 1
    assert second.cii_computed == 0

    cii_envelopes = broadcaster.of_type(ENVELOPE_CII)
    assert len(cii_envelopes) == 2  # one per batch, including the suppressed one
    assert all(e["data"]["origin_asset"] == "City_Payment_Gateway" for e in cii_envelopes)


def test_alert_debounce_zero_disables_dedup():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False] * 3),
        tripwire_signal=lambda f: True,
        alert_asset_debounce_sec=0.0,
    )
    result = pipeline([make_flow(f"f:{i}") for i in range(3)], make_meta())
    assert result.alerts_created == 3


# ---------------------------------------------------------------------------
# CII debounce / cache
# ---------------------------------------------------------------------------


def test_cii_computed_once_then_reused_within_debounce_window():
    clock = {"t": 0.0}
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True] * 4),
        cii_debounce_sec=30.0,
        alert_asset_debounce_sec=0.0,
        clock=lambda: clock["t"],
    )
    result = pipeline([make_flow(f"f:{i}") for i in range(4)], make_meta())
    assert result.cii_computed == 1
    assert result.cii_reused == 3
    assert len(session.snapshots()) == 1


def test_cii_recomputed_after_debounce_expires():
    clock = {"t": 0.0}
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True]),
        cii_debounce_sec=30.0,
        clock=lambda: clock["t"],
    )
    pipeline([make_flow("f:1")], make_meta(1))
    clock["t"] = 31.0
    result = pipeline([make_flow("f:2")], make_meta(2))
    assert result.cii_computed == 1


def test_cii_skipped_for_assets_absent_from_the_dependency_graph():
    """AssetRegistry auto-registers one Unresolved_<ip> per unique IP (T5)
    and real CIC-IDS2017 has thousands. Those nodes have no graph edges, so
    the Monte Carlo is guaranteed to return zeros — running it is waste."""
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True]),
    )
    result = pipeline([make_flow("f:1", src_ip="203.0.113.55")], make_meta())
    assert result.anomalies == 1
    assert result.cii_computed == 0
    assert session.snapshots() == []


def test_default_registry_resolves_curated_assets():
    """Regression: the bare `AssetRegistry()` constructor builds an EMPTY
    registry, so every curated smart-city IP falls through to
    auto-discovery and resolves to Unresolved_<ip>. That failure is silent
    — events still persist and the feed still scrolls — but every asset
    name is wrong, every CII is zero, and no alert can name a real asset.
    The pipeline must default to AssetRegistry.from_config()."""
    pipeline, session = make_pipeline()
    pipeline([make_flow("f:1", src_ip=GRAPH_ASSET_IP)], make_meta())
    row = rows_of(stmts_for(session, "events")[0])[0]
    assert row["source_asset"] == "City_Payment_Gateway"
    assert not row["source_asset"].startswith("Unresolved_")
    assert row["source_asset"] in build_criticality_map()


def test_cii_computed_for_a_real_graph_asset():
    pipeline, session = make_pipeline(scorer=FakeScorer(anomaly_flags=[True]))
    result = pipeline([make_flow("f:1", src_ip=GRAPH_ASSET_IP)], make_meta())
    assert result.cii_computed == 1
    snap = session.snapshots()[0]
    assert snap.origin_asset in build_criticality_map()
    assert isinstance(snap.impacted, dict)
    assert "assets" in snap.impacted


def test_cii_cache_is_bounded():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[True] * 6),
        cii_cache_max_entries=2,
        alert_asset_debounce_sec=0.0,
    )
    crit = list(build_criticality_map().keys())
    from config import SMART_CITY_ASSETS

    ips = [a["ip"] for a in SMART_CITY_ASSETS if a["asset_name"] in crit][:6]
    pipeline([make_flow(f"f:{i}", src_ip=ip) for i, ip in enumerate(ips)], make_meta())
    assert pipeline.cii_cache_size() <= 2


def test_cii_survives_alert_suppression():
    """Blast radius is an analytical record in its own right — suppressing
    the noisy volumetric alert channel must not discard it."""
    pipeline, session = make_pipeline(scorer=FakeScorer(anomaly_flags=[True]))
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.alerts_created == 0
    assert result.cii_computed == 1
    assert len(session.snapshots()) == 1


def test_alert_links_to_its_cii_snapshot():
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: True,
    )
    pipeline([make_flow("f:1")], make_meta())
    alert = session.alerts()[0]
    snapshot = session.snapshots()[0]
    assert alert.cii_snapshot_id == snapshot.id


# ---------------------------------------------------------------------------
# Persistence details
# ---------------------------------------------------------------------------


def test_events_inserted_in_batch_order():
    """The state board's Note for Ticket #8 depends on this: hundreds of
    events share one minute-granularity ts, so ORDER BY ts DESC, id DESC is
    only meaningful if ingest inserts in arrival order."""
    pipeline, session = make_pipeline(scorer=FakeScorer(anomaly_flags=[False] * 5))
    flows = [make_flow(f"f:{i}") for i in range(5)]
    pipeline(flows, make_meta())
    rows = rows_of(stmts_for(session, "events")[0])
    assert [r["source_row_id"] for r in rows] == [f"f:{i}" for i in range(5)]


def test_three_timestamps_never_collapsed():
    """Decision D5: ts (event time) and observed_at (detection time) are
    distinct columns; ingested_at is a server default."""
    pipeline, session = make_pipeline()
    meta = make_meta()
    pipeline([make_flow("f:1")], meta)
    row = rows_of(stmts_for(session, "events")[0])[0]
    assert row["ts"] == BASE_TS
    assert row["observed_at"] == meta.emitted_at
    assert row["ts"] != row["observed_at"]
    assert "ingested_at" not in row


def test_deduplicated_rows_get_no_duplicate_scores():
    """ON CONFLICT DO NOTHING returns no id for a row that already existed;
    writing its scores again would orphan duplicates on the original."""
    session = FakeSession(next_event_ids=[1])  # only the first row inserted
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False, False]),
        session=session,
        hybrid_enabled=False,  # this test is about dedup, not the hybrid
        # layer's own row -- see test_hybrid_row_survives_deduplication_too
        # in tests/test_ingest_hybrid.py for the hybrid-enabled equivalent
    )
    result = pipeline([make_flow("f:1"), make_flow("f:2")], make_meta())
    assert result.events_inserted == 1
    assert result.events_deduplicated == 1
    rows = rows_of(stmts_for(session, "event_scores")[0])
    assert len(rows) == 1
    assert rows[0]["event_id"] == 1


def test_inserted_plus_deduplicated_equals_received():
    session = FakeSession(next_event_ids=[1, 2])
    pipeline, session = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False] * 5), session=session
    )
    result = pipeline([make_flow(f"f:{i}") for i in range(5)], make_meta())
    assert result.events_inserted + result.events_deduplicated == result.flows_received


def test_raw_column_carries_ports_and_label():
    pipeline, session = make_pipeline()
    pipeline([make_flow("f:1", label="Bot", is_attack=True)], make_meta())
    raw = rows_of(stmts_for(session, "events")[0])[0]["raw"]
    assert raw["label"] == "Bot"
    assert raw["is_attack"] is True
    assert raw["source_port"] == 443


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


def test_event_envelope_shape():
    broadcaster = CollectingBroadcaster()
    pipeline, _ = make_pipeline(broadcaster=broadcaster)
    pipeline([make_flow("f:1")], make_meta())
    env = broadcaster.of_type(ENVELOPE_EVENT)[0]
    assert set(env.keys()) == {"type", "data"}
    assert env["type"] == ENVELOPE_EVENT
    for key in ("id", "ts", "source_asset", "calibrated_score", "is_anomaly"):
        assert key in env["data"]


def test_alert_and_cii_envelopes_published():
    broadcaster = CollectingBroadcaster()
    pipeline, _ = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: True,
        broadcaster=broadcaster,
    )
    pipeline([make_flow("f:1")], make_meta())
    assert len(broadcaster.of_type(ENVELOPE_ALERT)) == 1
    assert len(broadcaster.of_type(ENVELOPE_CII)) == 1


def test_deduplicated_events_are_not_rebroadcast():
    """Re-pushing them would make the live feed show a burst of repeats
    after any ingest retry."""
    session = FakeSession(next_event_ids=[1])
    broadcaster = CollectingBroadcaster()
    pipeline, _ = make_pipeline(
        scorer=FakeScorer(anomaly_flags=[False, False]),
        session=session,
        broadcaster=broadcaster,
    )
    pipeline([make_flow("f:1"), make_flow("f:2")], make_meta())
    assert len(broadcaster.of_type(ENVELOPE_EVENT)) == 1


def test_broadcast_failure_does_not_raise_or_lose_data():
    """Broadcasting happens after commit; a dead WebSocket must never roll
    back data already durable in Postgres."""

    class ExplodingBroadcaster:
        def publish(self, envelope):
            raise RuntimeError("socket closed")

    pipeline, session = make_pipeline(broadcaster=ExplodingBroadcaster())
    result = pipeline([make_flow("f:1")], make_meta())
    assert result.events_inserted == 1
    assert pipeline.stats().broadcast_failures == 1


def test_null_broadcaster_is_the_default():
    scorer = FakeScorer()

    @contextmanager
    def factory():
        yield FakeSession()

    pipeline = IngestPipeline(scorer=scorer, session_factory=factory)
    assert isinstance(pipeline._broadcaster, NullBroadcaster)
    pipeline([make_flow("f:1")], make_meta())


def test_collecting_broadcaster_is_bounded():
    b = CollectingBroadcaster(max_entries=3)
    for i in range(10):
        b.publish({"type": ENVELOPE_EVENT, "data": {"i": i}})
    assert len(b.envelopes) == 3
    assert b.envelopes[-1]["data"]["i"] == 9


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


def test_persistence_failure_raises_so_engine_counters_stay_honest():
    """ReplayEngine catches consumer exceptions and increments
    consumer_error_count. Swallowing a DB error here would report a healthy
    replay that persisted nothing."""

    class ExplodingSession(FakeSession):
        def execute(self, stmt):
            raise RuntimeError("connection lost")

    session = ExplodingSession()

    @contextmanager
    def factory():
        yield session

    pipeline = IngestPipeline(scorer=FakeScorer(), session_factory=factory)
    with pytest.raises(RuntimeError, match="connection lost"):
        pipeline([make_flow("f:1")], make_meta())


# ---------------------------------------------------------------------------
# Retention wiring (Ticket #2 deferred this to Ticket #7)
# ---------------------------------------------------------------------------


def test_retention_runs_on_the_configured_cadence(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "backend.ingest.prune_events", lambda session, *a, **k: calls.append(1) or 0
    )
    pipeline, _ = make_pipeline(retention_check_every_n_batches=3)
    for i in range(6):
        pipeline([make_flow(f"f:{i}")], make_meta(i))
    assert len(calls) == 2  # batches 3 and 6


def test_retention_not_run_every_batch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "backend.ingest.prune_events", lambda session, *a, **k: calls.append(1) or 0
    )
    pipeline, _ = make_pipeline(retention_check_every_n_batches=1000)
    for i in range(10):
        pipeline([make_flow(f"f:{i}")], make_meta(i))
    assert calls == []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_accumulate_across_batches():
    pipeline, _ = make_pipeline(scorer=FakeScorer(anomaly_flags=[False, False]))
    pipeline([make_flow("a:1"), make_flow("a:2")], make_meta(1))
    pipeline([make_flow("b:1"), make_flow("b:2")], make_meta(2))
    stats = pipeline.stats()
    assert stats.batches == 2
    assert stats.flows_received == 4


def test_stats_returns_a_snapshot_not_the_live_object():
    pipeline, _ = make_pipeline()
    first = pipeline.stats()
    pipeline([make_flow("f:1")], make_meta())
    assert first.batches == 0
    assert pipeline.stats().batches == 1


# ---------------------------------------------------------------------------
# Invariant B — this module must never fit a model
# ---------------------------------------------------------------------------


def test_ingest_never_calls_fit(monkeypatch):
    """Guards the seam Ticket #5 pinned inside StreamingScorer: ingest is
    the caller that could reintroduce a per-batch refit."""
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler

    def boom(*a, **k):
        raise AssertionError("Invariant B violated: fit called in the ingest path")

    monkeypatch.setattr(StandardScaler, "fit", boom)
    monkeypatch.setattr(StandardScaler, "fit_transform", boom)
    monkeypatch.setattr(IsolationForest, "fit", boom)

    pipeline, _ = make_pipeline(scorer=FakeScorer(anomaly_flags=[True] * 3))
    pipeline([make_flow(f"f:{i}") for i in range(3)], make_meta())


# ---------------------------------------------------------------------------
# Live-DB tests — opt-in only (AEGIS_TEST_LIVE_DB=1), skipped by default.
# ---------------------------------------------------------------------------

_LIVE_DB = os.environ.get("AEGIS_TEST_LIVE_DB") == "1"

pytestmark_live = pytest.mark.skipif(
    not _LIVE_DB,
    reason="Live-DB tests are opt-in: set AEGIS_TEST_LIVE_DB=1 (requires local Postgres)",
)


@pytest.fixture()
def live_pipeline_session():
    if not _LIVE_DB:
        pytest.skip("AEGIS_TEST_LIVE_DB not set")
    from sqlalchemy import delete

    from backend.db import get_engine, get_session_factory
    from backend.models import Base, Event

    engine = get_engine()
    Base.metadata.create_all(engine)
    factory = get_session_factory()
    session_id = uuid.uuid4()
    yield factory, session_id
    # Clean up only this test's rows. event_scores cascade with the event;
    # snapshots/alerts are cleaned explicitly since they SET NULL instead.
    with factory() as s:
        ids = [
            e.id
            for e in s.query(Event).filter(Event.replay_session_id == session_id).all()
        ]
        if ids:
            s.execute(delete(CiiSnapshot).where(CiiSnapshot.trigger_event_id.in_(ids)))
        s.execute(delete(Event).where(Event.replay_session_id == session_id))
        s.commit()


@pytestmark_live
def test_live_roundtrip_persists_events_and_scores(live_pipeline_session):
    from sqlalchemy import select

    from backend.models import Event, EventScore

    factory, session_id = live_pipeline_session

    @contextmanager
    def scoped():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    pipeline = IngestPipeline(
        scorer=FakeScorer(anomaly_flags=[False, True, False]),
        session_factory=scoped,
        # This test is about DB roundtrip integrity (real FK linkage, real
        # timestamp population, real row counts against Postgres) -- not
        # about the hybrid layer's own separately-tested row. Disabled so
        # the exact-count assertion below stays meaningful; mirrors the
        # same hybrid_enabled=False scoping already applied to this file's
        # non-live row-count tests (test_tripwire_score_row_only_written_
        # when_it_fires, test_no_supervised_row_when_channel_not_
        # configured, test_deduplicated_rows_get_no_duplicate_scores).
        hybrid_enabled=False,
    )
    flows = [make_flow(f"live:{i}", ts_offset=i) for i in range(3)]
    result = pipeline(flows, make_meta(session_id=session_id))
    assert result.events_inserted == 3

    with factory() as s:
        events = s.scalars(
            select(Event)
            .where(Event.replay_session_id == session_id)
            .order_by(Event.id)
        ).all()
        assert [e.source_row_id for e in events] == ["live:0", "live:1", "live:2"]
        # D5: three distinct timestamps all populated.
        assert all(e.ts is not None and e.observed_at is not None for e in events)
        assert all(e.ingested_at is not None for e in events)
        scores = s.scalars(
            select(EventScore).where(EventScore.event_id.in_([e.id for e in events]))
        ).all()
        assert len(scores) == 3


@pytestmark_live
def test_live_redelivery_is_deduplicated_not_duplicated(live_pipeline_session):
    """D4: (replay_session_id, source_row_id) UNIQUE makes an ingest retry
    a no-op instead of a duplicate-key crash."""
    from sqlalchemy import func, select

    from backend.models import Event

    factory, session_id = live_pipeline_session

    @contextmanager
    def scoped():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    pipeline = IngestPipeline(scorer=FakeScorer(), session_factory=scoped)
    meta = make_meta(session_id=session_id)
    flows = [make_flow(f"live:{i}") for i in range(3)]

    first = pipeline(flows, meta)
    second = pipeline(flows, meta)

    assert first.events_inserted == 3
    assert second.events_inserted == 0
    assert second.events_deduplicated == 3

    with factory() as s:
        count = s.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.replay_session_id == session_id)
        )
        assert count == 3


@pytestmark_live
def test_live_alert_links_to_snapshot_and_event(live_pipeline_session):
    from sqlalchemy import select

    from backend.models import Alert as AlertModel

    factory, session_id = live_pipeline_session

    @contextmanager
    def scoped():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    pipeline = IngestPipeline(
        scorer=FakeScorer(anomaly_flags=[False]),
        tripwire_signal=lambda f: True,
        session_factory=scoped,
    )
    result = pipeline([make_flow("live:0", src_ip=GRAPH_ASSET_IP)], make_meta(session_id=session_id))
    assert result.alerts_created == 1

    with factory() as s:
        alert = s.scalars(
            select(AlertModel).order_by(AlertModel.id.desc()).limit(1)
        ).one()
        assert alert.cii_snapshot_id is not None
        snapshot = s.get(CiiSnapshot, alert.cii_snapshot_id)
        assert snapshot.trigger_event_id is not None
        assert isinstance(alert.explanation, dict)
        s.delete(alert)
        s.commit()
