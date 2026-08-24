"""
tests/test_replay_engine.py — Phase 5 Ticket #6: `backend.replay_engine`.

Determinism strategy (per the ticket's explicit requirement: no test may
depend on wall-clock timing being precise):

1. Pure functions (`compute_virtual_times`, `wall_target`, `due_index`) are
   tested directly with plain floats/datetimes — no engine, no thread, no
   clock at all.
2. Engine *scheduling* behaviour (bursts, chronological order, injection,
   re-anchoring, lag) is tested by constructing a `ReplayEngine` with an
   injected fake clock and calling its `_tick_once(now)` step directly,
   advancing the fake clock manually between calls. No real thread, no
   real sleeping, fully deterministic.
3. Only thread *lifecycle* tests (start/stop idempotency, no thread leak,
   session id changes across start()/loop) exercise the real background
   thread — with a tiny synthetic dataset and a small real
   `tick_interval`, asserting structural properties (thread state, call
   counts) rather than precise timing.
4. A real-data smoke test is skipped cleanly if `datasets/TrafficLabelling `
   is absent, matching tests/test_replay_reader.py's pattern.
"""
from __future__ import annotations

import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from backend.config import BackendSettings  # noqa: E402
from backend.replay_engine import (  # noqa: E402
    BATCH_ORIGIN_INJECTED,
    BATCH_ORIGIN_REPLAY,
    ReplayEngine,
    ReplayEngineError,
    compute_virtual_times,
    due_index,
    wall_target,
)
from backend.replay_reader import ReplayFlow, ReplayFlowReader  # noqa: E402

UTC = timezone.utc


def _flow(
    ts: datetime,
    source_ip: str = "10.0.0.1",
    source_row_id: str = "synthetic:1",
    label: str = "BENIGN",
) -> ReplayFlow:
    return ReplayFlow(
        ts=ts,
        source_ip=source_ip,
        source_port=1234,
        destination_ip="10.0.0.9",
        destination_port=80,
        protocol="TCP",
        duration_sec=0.1,
        packets=1,
        bytes=100,
        label=label,
        is_attack=label.strip().upper() != "BENIGN",
        timing_provenance="interpolated_minute_bucket",
        source_row_id=source_row_id,
        source_dataset="synthetic",
    )


class _FakeClock:
    """A manually-advanced monotonic-style clock for deterministic tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt


class _FakeReader:
    """A spy `ReplayFlowReader` substitute: returns a fixed flow list for
    any day and counts how many times `iter_flows()` was actually called,
    for the preload()-avoids-reread assertion.
    """

    def __init__(self, flows: list[ReplayFlow]) -> None:
        self._flows = flows
        self.call_count = 0

    def iter_flows(self, day=None, limit=None):
        self.call_count += 1
        flows = self._flows
        if limit is not None:
            flows = flows[:limit]
        yield from flows


# ---------------------------------------------------------------------------
# 1. compute_virtual_times — pure, no engine involved
# ---------------------------------------------------------------------------


def test_virtual_times_evenly_spaced_and_within_bucket():
    base = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    next_ts = datetime(2017, 7, 7, 9, 1, 0, tzinfo=UTC)  # 60s gap
    n = 10
    timestamps = [base] * n + [next_ts]
    virtual = compute_virtual_times(timestamps)

    assert len(virtual) == n + 1
    # Strictly non-decreasing overall.
    assert all(virtual[i] <= virtual[i + 1] for i in range(len(virtual) - 1))
    # Strictly increasing within the run (gap > 0).
    run = virtual[:n]
    assert all(run[i] < run[i + 1] for i in range(n - 1))
    # Evenly spaced: each step is gap/N.
    step = 60.0 / n
    for i, v in enumerate(run):
        assert v == pytest.approx(base.timestamp() + i * step)
    # Stay within [T_k, T_{k+1}).
    assert run[0] == pytest.approx(base.timestamp())
    assert run[-1] < next_ts.timestamp()
    # The lone final entry (next_ts) is its own one-element run.
    assert virtual[-1] == pytest.approx(next_ts.timestamp())


def test_virtual_times_final_run_reuses_previous_gap():
    t0 = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    t1 = datetime(2017, 7, 7, 9, 0, 10, tzinfo=UTC)  # gap 10s
    # Final run: 4 flows sharing t1, with no distinct timestamp after it.
    timestamps = [t0, t1, t1, t1, t1]
    virtual = compute_virtual_times(timestamps)
    # gap reused for the final run is the 10s gap between t0 and t1.
    expected_step = 10.0 / 4
    final_run = virtual[1:]
    for i, v in enumerate(final_run):
        assert v == pytest.approx(t1.timestamp() + i * expected_step)


def test_virtual_times_single_timestamp_whole_list():
    t0 = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    virtual = compute_virtual_times([t0] * 5)
    # No gap information exists anywhere -> gap defaults to 0, all equal.
    assert virtual == [t0.timestamp()] * 5


def test_virtual_times_empty():
    assert compute_virtual_times([]) == []


def test_virtual_times_no_duplicates_is_identity_shape():
    ts = [
        datetime(2017, 7, 7, 9, 0, i, tzinfo=UTC) for i in range(5)
    ]
    virtual = compute_virtual_times(ts)
    assert virtual == [t.timestamp() for t in ts]


# ---------------------------------------------------------------------------
# 2. wall_target / due_index — pure
# ---------------------------------------------------------------------------


def test_wall_target_fixed_anchor_formula():
    anchor_wall = 1000.0
    anchor_virtual = 500.0
    speed = 10.0
    # 50 virtual seconds ahead of anchor -> 5 wall seconds ahead at 10x.
    assert wall_target(550.0, anchor_wall, anchor_virtual, speed) == pytest.approx(1005.0)
    assert wall_target(500.0, anchor_wall, anchor_virtual, speed) == pytest.approx(1000.0)


def test_due_index_basic():
    virtual_times = [0.0, 1.0, 2.0, 3.0, 4.0]
    # speed=1, anchor at wall=0/virtual=0: now=2.5 -> due up to index where
    # virtual_time <= 2.5, i.e. indices 0,1,2 (values 0,1,2) => idx 3.
    idx = due_index(virtual_times, 0, anchor_wall=0.0, anchor_virtual=0.0, speed=1.0, now_wall=2.5)
    assert idx == 3


def test_due_index_respects_pointer_lower_bound():
    virtual_times = [0.0, 1.0, 2.0, 3.0, 4.0]
    idx = due_index(virtual_times, 2, anchor_wall=0.0, anchor_virtual=0.0, speed=1.0, now_wall=10.0)
    assert idx == 5
    idx2 = due_index(virtual_times, 5, anchor_wall=0.0, anchor_virtual=0.0, speed=1.0, now_wall=10.0)
    assert idx2 == 5  # already exhausted, no-op


def test_due_index_speed_scaling():
    virtual_times = [0.0, 10.0, 20.0, 30.0]
    # At speed=10, 1 wall second == 10 virtual seconds.
    idx = due_index(virtual_times, 0, anchor_wall=0.0, anchor_virtual=0.0, speed=10.0, now_wall=1.0)
    assert idx == 2  # virtual now = 10 -> flows at 0 and 10 are due


# ---------------------------------------------------------------------------
# 3. Engine scheduling via _tick_once() + fake clock — no threads
# ---------------------------------------------------------------------------


def _make_engine(flows, consumer, clock=None, **kwargs):
    reader = _FakeReader(flows)
    engine = ReplayEngine(consumer=consumer, reader=reader, clock=clock, **kwargs)
    return engine, reader


def test_ts_never_mutated():
    """Pin the honesty requirement: emitted flows' ts is byte-identical
    to the source ts, never the interpolated virtual time."""
    base = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    flows = [_flow(base, source_row_id=f"f:{i}") for i in range(50)]
    original_ts = [f.ts for f in flows]

    seen = []

    def consumer(batch, meta):
        seen.extend(batch)

    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, consumer, clock=clock, tick_interval=0.05)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times(original_ts)
    engine._day = "d"
    engine._speed = 1.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]
    clock.advance(1000.0)  # fire everything at once
    engine._tick_once(clock())

    assert len(seen) == len(flows)
    assert [f.ts for f in seen] == original_ts


def test_no_burst_on_dense_bucket():
    """T3 regression test: a dense bucket (many flows sharing one ts) must
    not all fire in a single tick when ticks are frequent relative to the
    bucket's spread-out wall-clock duration."""
    t0 = datetime(2017, 7, 7, 10, 30, 0, tzinfo=UTC)
    t1 = datetime(2017, 7, 7, 10, 31, 0, tzinfo=UTC)  # 60s gap
    n = 4017  # measured friday-morning max bucket size
    flows = [_flow(t0, source_row_id=f"f:{i}") for i in range(n)]
    flows.append(_flow(t1, source_row_id="f:last"))

    batch_sizes = []

    def consumer(batch, meta):
        batch_sizes.append(len(batch))

    clock = _FakeClock(0.0)
    tick_interval = 0.1
    speed = 20.0  # matches Fact B's measured speed
    engine, _ = _make_engine(flows, consumer, clock=clock, tick_interval=tick_interval)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times([f.ts for f in flows])
    engine._day = "d"
    engine._speed = speed
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    # 60s of virtual time at 20x == 3s of wall time. Tick every 0.1s wall
    # (30 ticks) until the whole bucket has been emitted.
    for _ in range(40):
        clock.advance(tick_interval)
        engine._tick_once(clock())
        if sum(batch_sizes) >= n:
            break

    assert sum(batch_sizes) >= n
    # The T3 assertion: no single tick emitted the whole (or even most of
    # the) dense bucket — contrast with naive delta-pacing, which would
    # fire all 4017 in one instant (see the verification report).
    assert max(batch_sizes) < n
    assert len(batch_sizes) > 1


def test_batch_size_capped_no_loss_and_ordered():
    """MEDIUM-1 regression: with a small `max_batch_size` and a dense
    synthetic bucket (all flows sharing one timestamp, so every flow is
    "due" the instant the clock is advanced), no emitted batch may ever
    exceed the cap, no flow may be lost, and the carried-forward
    remainder must appear in subsequent batches in original order."""
    t0 = datetime(2017, 7, 7, 10, 30, 0, tzinfo=UTC)
    n = 1000
    flows = [_flow(t0, source_row_id=f"f:{i}") for i in range(n)]

    batch_sizes = []
    seen_ids: list[str] = []

    def consumer(batch, meta):
        batch_sizes.append(len(batch))
        seen_ids.extend(f.source_row_id for f in batch)

    cap = 50
    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, consumer, clock=clock, max_batch_size=cap)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times([f.ts for f in flows])
    engine._day = "d"
    engine._speed = 1.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    clock.advance(10_000.0)  # every flow is "due" from this point on
    for _ in range(n // cap + 5):
        engine._tick_once(clock())
        if sum(batch_sizes) >= n:
            break

    # No loss: total emitted across all batches equals the input count.
    assert sum(batch_sizes) == n
    assert len(seen_ids) == n
    # Cap respected on every single batch.
    assert max(batch_sizes) <= cap
    assert len(batch_sizes) == n // cap  # exactly n/cap batches of exactly cap
    assert all(size == cap for size in batch_sizes)
    # Chronological order preserved end-to-end.
    assert seen_ids == [f"f:{i}" for i in range(n)]
    # The carried remainder appears in the *next* batch, in order: batch 0
    # is f:0..f:49, batch 1 is f:50..f:99, etc.
    for batch_idx in range(len(batch_sizes)):
        start = batch_idx * cap
        end = start + cap
        assert seen_ids[start:end] == [f"f:{i}" for i in range(start, end)]


def test_chronological_order_preserved_with_cap_active():
    """Ordering-preservation regression for MEDIUM-1: capping batches
    across many ticks with distinct (non-shared) timestamps must not
    reorder or interleave flows — overall emission stays chronological."""
    rng_ts = [
        datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC) + timedelta(seconds=i // 3)
        for i in range(300)
    ]
    flows = [_flow(ts, source_row_id=f"f:{i}") for i, ts in enumerate(rng_ts)]

    emitted_ts = []
    batch_sizes = []

    def consumer(batch, meta):
        emitted_ts.extend(f.ts for f in batch)
        batch_sizes.append(len(batch))

    cap = 10
    clock = _FakeClock(0.0)
    engine, _ = _make_engine(
        flows, consumer, clock=clock, tick_interval=0.1, max_batch_size=cap
    )
    engine._flows = flows
    engine._virtual_times = compute_virtual_times(rng_ts)
    engine._day = "d"
    engine._speed = 50.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    for _ in range(400):
        clock.advance(0.1)
        engine._tick_once(clock())
        if len(emitted_ts) >= len(flows):
            break

    assert len(emitted_ts) == len(flows)
    assert emitted_ts == sorted(emitted_ts)
    assert max(batch_sizes) <= cap


def test_chronological_emission_across_run():
    """Batches arrive in non-decreasing ts order across the whole run."""
    rng_ts = [
        datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC) + timedelta(seconds=i // 3)
        for i in range(300)
    ]
    flows = [_flow(ts, source_row_id=f"f:{i}") for i, ts in enumerate(rng_ts)]

    emitted_ts = []

    def consumer(batch, meta):
        emitted_ts.extend(f.ts for f in batch)

    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, consumer, clock=clock, tick_interval=0.1)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times(rng_ts)
    engine._day = "d"
    engine._speed = 50.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    for _ in range(200):
        clock.advance(0.1)
        engine._tick_once(clock())
        if len(emitted_ts) >= len(flows):
            break

    assert len(emitted_ts) == len(flows)
    assert emitted_ts == sorted(emitted_ts)


def test_speed_reanchor_no_backward_jump_or_stall():
    """Changing speed mid-run must not cause a pending flow's wall_target
    to jump backward (already-passed) or stall far into the future."""
    t0 = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    flows = [_flow(t0 + timedelta(seconds=i), source_row_id=f"f:{i}") for i in range(100)]

    def consumer(batch, meta):
        pass

    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, consumer, clock=clock)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times([f.ts for f in flows])
    engine._day = "d"
    engine._speed = 1.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    # Before re-anchor: target of flow index 50 at speed 1.0.
    before = wall_target(
        engine._virtual_times[50], engine._anchor_wall, engine._anchor_virtual, engine._speed
    )

    clock.advance(10.0)  # 10 wall seconds pass at speed 1.0
    engine.set_speed(5.0)

    # Immediately after re-anchoring (no further wall time passed), the
    # target for a flow far in the future should not have jumped
    # backward past "now", and should be finite / reachable, not stalled
    # at +inf-like distance.
    now = clock()
    after = wall_target(
        engine._virtual_times[50], engine._anchor_wall, engine._anchor_virtual, engine._speed
    )
    # The flow at index 50 (virtual offset 50s from start) was already
    # due before re-anchoring (10 virtual/wall seconds elapsed at speed
    # 1x covers up to index 10's timestamp, not 50 — so index 50 is still
    # pending both before and after). Its target must not have moved
    # into the past relative to "now".
    assert after >= now - 1e-9
    # And re-anchoring at a faster speed must pull the target closer
    # (smaller), not push it further away (stall).
    assert after <= before


def test_speed_reanchor_pending_flow_emits_at_new_rate():
    """End-to-end via _tick_once: after set_speed(), remaining flows emit
    at the new pace, with no gap larger than a naive re-schedule."""
    t0 = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    flows = [_flow(t0 + timedelta(seconds=i), source_row_id=f"f:{i}") for i in range(20)]

    emitted = []

    def consumer(batch, meta):
        emitted.extend(batch)

    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, consumer, clock=clock, tick_interval=0.1)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times([f.ts for f in flows])
    engine._day = "d"
    engine._speed = 1.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    # A few ticks at speed 1x.
    for _ in range(20):
        clock.advance(0.1)
        engine._tick_once(clock())
    emitted_at_1x = len(emitted)
    assert 0 < emitted_at_1x < len(flows)

    # Speed way up; remaining flows should catch up quickly.
    engine.set_speed(100.0)
    for _ in range(50):
        clock.advance(0.1)
        engine._tick_once(clock())
        if len(emitted) >= len(flows):
            break

    assert len(emitted) == len(flows)
    assert [f.ts for f in emitted] == sorted(f.ts for f in emitted)


def test_lag_reported_when_now_exceeds_target():
    t0 = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    flows = [_flow(t0 + timedelta(seconds=i), source_row_id=f"f:{i}") for i in range(5)]

    def consumer(batch, meta):
        pass

    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, consumer, clock=clock, lag_warning_threshold=1.0)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times([f.ts for f in flows])
    engine._day = "d"
    engine._speed = 1.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    clock.advance(10.0)  # way past every flow's target -> big lag
    engine._tick_once(clock())

    status = engine.status()
    assert status.lag_seconds > 0.0
    assert status.emitted_count == len(flows)


def test_no_lag_when_on_schedule():
    t0 = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    flows = [_flow(t0, source_row_id="f:0")]

    def consumer(batch, meta):
        pass

    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, consumer, clock=clock)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times([f.ts for f in flows])
    engine._day = "d"
    engine._speed = 1.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    engine._tick_once(clock())  # now == anchor_wall, exactly on time
    assert engine.status().lag_seconds == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 4. inject()
# ---------------------------------------------------------------------------


def test_inject_emitted_promptly_and_tagged():
    t0 = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    flows = [_flow(t0 + timedelta(seconds=100), source_row_id="scheduled:0")]  # far future

    origins_seen = []

    def consumer(batch, meta):
        origins_seen.append((meta.origin, [f.source_row_id for f in batch]))

    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, consumer, clock=clock)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times([f.ts for f in flows])
    engine._day = "d"
    engine._speed = 1.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    # Anchor virtual time at t0 (not the scheduled flow's own, far-future
    # virtual time), so at now==anchor_wall the scheduled flow is not yet
    # due -- isolating this test to just the injection's promptness.
    engine._anchor_virtual = t0.timestamp()

    injected_flow = _flow(t0, source_row_id="injected:0")
    engine.inject([injected_flow])

    # No wall time has passed, so the scheduled flow is not due yet, but
    # the injection should still be emitted on the very next tick.
    engine._tick_once(clock())

    assert len(origins_seen) == 1
    origin, ids = origins_seen[0]
    assert origin == BATCH_ORIGIN_INJECTED
    assert ids == ["injected:0"]


def test_inject_queue_cap_raises():
    def consumer(batch, meta):
        pass

    engine, _ = _make_engine([], consumer, injection_queue_max=3)
    with pytest.raises(ReplayEngineError):
        engine.inject([_flow(datetime.now(UTC)) for _ in range(4)])


def test_inject_empty_list_is_noop():
    def consumer(batch, meta):
        pass

    engine, _ = _make_engine([], consumer)
    engine.inject([])  # must not raise


# ---------------------------------------------------------------------------
# 5. Consumer exception handling
# ---------------------------------------------------------------------------


def test_consumer_exception_does_not_stop_engine_and_is_counted():
    """MEDIUM-2 regression: a consumer that raises on one batch (the 1st,
    here) must have that batch's flows counted in
    `consumer_failed_flow_count` (distinct from `consumer_error_count`,
    which counts batches), while `emitted_count` still counts every flow
    the engine handed off — and replay must continue normally afterwards
    (the 2nd batch succeeds and is reflected in status())."""
    t0 = datetime(2017, 7, 7, 9, 0, 0, tzinfo=UTC)
    flows = [_flow(t0 + timedelta(seconds=i), source_row_id=f"f:{i}") for i in range(5)]

    calls = []
    state = {"raised": False}

    def flaky_consumer(batch, meta):
        calls.append(batch)
        if not state["raised"]:
            state["raised"] = True
            raise ValueError("boom")

    clock = _FakeClock(0.0)
    engine, _ = _make_engine(flows, flaky_consumer, clock=clock)
    engine._flows = flows
    engine._virtual_times = compute_virtual_times([f.ts for f in flows])
    engine._day = "d"
    engine._speed = 1.0
    engine._session_id = uuid.uuid4()
    engine._anchor_wall = clock()
    engine._anchor_virtual = engine._virtual_times[0]

    clock.advance(0.5)  # only f:0 due -> 1st batch (1 flow), consumer raises
    engine._tick_once(clock())

    clock.advance(10.0)  # remaining 4 flows now due -> 2nd batch, succeeds
    engine._tick_once(clock())

    assert len(calls) == 2  # both batches were handed to the consumer
    status = engine.status()
    assert status.consumer_error_count == 1  # exactly one failing batch
    assert status.consumer_failed_flow_count == 1  # == len(1st batch)
    # emitted_count counts every flow scheduled/handed off, success or not.
    assert status.emitted_count == 5
    # Successfully-processed flows = emitted_count - consumer_failed_flow_count.
    assert status.emitted_count - status.consumer_failed_flow_count == 4
    # Replay continued: the 2nd batch's 4 flows made it through without
    # raising, and the pointer reflects the whole run being scheduled.
    assert len(calls[1]) == 4


# ---------------------------------------------------------------------------
# 6. preload() avoids re-reading
# ---------------------------------------------------------------------------


def test_preload_then_start_does_not_reread():
    flows = [_flow(datetime(2017, 7, 7, 9, 0, i, tzinfo=UTC), source_row_id=f"f:{i}") for i in range(5)]

    def consumer(batch, meta):
        pass

    engine, reader = _make_engine(flows, consumer)
    engine.preload(day="d")
    assert reader.call_count == 1

    engine.start(day="d", speed=1000.0)
    engine.stop()
    assert reader.call_count == 1  # start() must not have re-read

    engine.start(day="d", speed=1000.0)
    engine.stop()
    assert reader.call_count == 1  # second start(), still cached


def test_preload_is_noop_if_already_cached():
    flows = [_flow(datetime.now(UTC))]

    def consumer(batch, meta):
        pass

    engine, reader = _make_engine(flows, consumer)
    engine.preload(day="d")
    engine.preload(day="d")
    assert reader.call_count == 1


# ---------------------------------------------------------------------------
# 7. Real-thread lifecycle tests — tiny synthetic dataset, small real ticks
# ---------------------------------------------------------------------------


def test_start_stop_idempotent_and_no_thread_leak():
    flows = [_flow(datetime.now(UTC) + timedelta(milliseconds=i)) for i in range(5)]

    def consumer(batch, meta):
        pass

    engine, _ = _make_engine(flows, consumer, tick_interval=0.01)
    engine.start(day="d", speed=1000.0)
    thread = engine._thread
    engine.stop()
    engine.stop()  # idempotent, must not raise

    assert thread is not None
    assert not thread.is_alive()
    assert engine.status().running is False


def test_start_while_running_raises():
    flows = [_flow(datetime.now(UTC) + timedelta(seconds=i)) for i in range(1000)]

    def consumer(batch, meta):
        pass

    engine, _ = _make_engine(flows, consumer, tick_interval=0.01)
    engine.start(day="d", speed=1.0)
    try:
        with pytest.raises(ReplayEngineError):
            engine.start(day="d", speed=1.0)
    finally:
        engine.stop()


def test_session_id_changes_across_start_calls():
    flows = [_flow(datetime.now(UTC) + timedelta(milliseconds=i)) for i in range(3)]

    def consumer(batch, meta):
        pass

    engine, _ = _make_engine(flows, consumer, tick_interval=0.01)
    sid1 = engine.start(day="d", speed=1000.0)
    engine.stop()
    sid2 = engine.start(day="d", speed=1000.0)
    engine.stop()

    assert isinstance(sid1, uuid.UUID)
    assert sid1 != sid2


def test_session_id_changes_across_loop_iterations():
    base = datetime.now(UTC)
    flows = [_flow(base + timedelta(milliseconds=i), source_row_id=f"f:{i}") for i in range(3)]

    session_ids_seen = set()
    done = threading.Event()

    def consumer(batch, meta):
        session_ids_seen.add(meta.replay_session_id)
        if len(session_ids_seen) >= 2:
            done.set()

    engine, _ = _make_engine(flows, consumer, tick_interval=0.01)
    engine.start(day="d", speed=10000.0, loop=True)
    done.wait(timeout=10.0)
    engine.stop()

    assert len(session_ids_seen) >= 2


def test_no_thread_leak_after_natural_exhaustion():
    flows = [_flow(datetime.now(UTC) + timedelta(milliseconds=i)) for i in range(3)]
    finished = threading.Event()

    def consumer(batch, meta):
        pass

    engine, _ = _make_engine(flows, consumer, tick_interval=0.01)
    engine.start(day="d", speed=10000.0, loop=False)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and engine.status().running:
        time.sleep(0.01)

    assert engine.status().running is False
    thread = engine._thread
    if thread is not None:
        thread.join(timeout=2.0)
        assert not thread.is_alive()
    engine.stop()  # idempotent cleanup


# ---------------------------------------------------------------------------
# 8. start_at / limit
# ---------------------------------------------------------------------------


def test_start_at_hhmm_skips_into_capture():
    day_date = datetime(2017, 7, 7, tzinfo=UTC)
    flows = [
        _flow(day_date.replace(hour=8, minute=59), source_row_id="a"),
        _flow(day_date.replace(hour=9, minute=34), source_row_id="b"),
        _flow(day_date.replace(hour=10, minute=0), source_row_id="c"),
    ]

    def consumer(batch, meta):
        pass

    engine, _ = _make_engine(flows, consumer, tick_interval=0.01)
    engine.start(day="d", speed=10000.0, start_at="09:34")
    engine.stop()
    assert [f.source_row_id for f in engine._flows] == ["b", "c"]


def test_limit_bounds_run_from_start_at():
    day_date = datetime(2017, 7, 7, tzinfo=UTC)
    flows = [
        _flow(day_date.replace(hour=9, minute=i), source_row_id=f"f:{i}") for i in range(10)
    ]

    def consumer(batch, meta):
        pass

    engine, _ = _make_engine(flows, consumer, tick_interval=0.01)
    engine.start(day="d", speed=10000.0, start_at="09:03", limit=2)
    engine.stop()
    assert [f.source_row_id for f in engine._flows] == ["f:3", "f:4"]
    assert engine.status().total_for_day == 10  # unaffected by limit


# ---------------------------------------------------------------------------
# 9. Real-data smoke test
# ---------------------------------------------------------------------------


def _require_real_dataset() -> ReplayFlowReader:
    reader = ReplayFlowReader()
    if not reader.data_dir.exists():
        pytest.skip(
            f"CIC-IDS2017 TrafficLabelling dataset not found at {reader.data_dir}; "
            "see docs/DATASETS.md."
        )
    return reader


def test_real_data_smoke_high_speed_small_limit():
    reader = _require_real_dataset()
    if "friday-morning" not in reader.available_days():
        pytest.skip("friday-morning capture file not present")

    LIMIT = 2000
    SPEED = 5000.0

    emitted = []
    batch_sizes = []
    origins = set()
    provenances = set()

    def consumer(batch, meta):
        emitted.extend(batch)
        batch_sizes.append(len(batch))
        origins.add(meta.origin)

    engine = ReplayEngine(consumer=consumer, reader=reader, tick_interval=0.02)
    engine.start(day="friday-morning", speed=SPEED, limit=LIMIT)

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and engine.status().running:
        time.sleep(0.02)
    engine.stop()

    assert len(emitted) == LIMIT
    ts_values = [f.ts for f in emitted]
    assert ts_values == sorted(ts_values)
    for f in emitted:
        provenances.add(f.timing_provenance)
        assert f.source_dataset  # provenance/source tagging present
    assert origins == {BATCH_ORIGIN_REPLAY}
    status = engine.status()
    assert status.emitted_count == LIMIT
    assert status.batches_emitted == len(batch_sizes)
    assert status.running is False


# ---------------------------------------------------------------------------
# 10. Misc — resolves BACKEND_SETTINGS defaults, matching CLAUDE.md
# optional-override convention
# ---------------------------------------------------------------------------


def test_engine_defaults_come_from_backend_settings():
    def consumer(batch, meta):
        pass

    custom_settings = BackendSettings(
        replay_tick_interval_sec=0.25,
        replay_lag_warning_threshold_sec=3.5,
        replay_injection_queue_max=42,
        replay_thread_join_timeout_sec=1.5,
    )
    import backend.replay_engine as replay_engine_module

    original = replay_engine_module.BACKEND_SETTINGS
    try:
        replay_engine_module.BACKEND_SETTINGS = custom_settings
        engine = ReplayEngine(consumer=consumer)
        assert engine._tick_interval == 0.25
        assert engine._lag_warning_threshold == 3.5
        assert engine._injection_queue_max == 42
        assert engine._thread_join_timeout == 1.5
    finally:
        replay_engine_module.BACKEND_SETTINGS = original


def test_explicit_overrides_win_over_settings():
    def consumer(batch, meta):
        pass

    engine = ReplayEngine(consumer=consumer, tick_interval=0.5)
    assert engine._tick_interval == 0.5
