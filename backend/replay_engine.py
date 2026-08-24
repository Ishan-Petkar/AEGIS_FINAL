"""
backend/replay_engine.py — Phase 5 Ticket #6: the replay engine.

Paces `backend.replay_reader.ReplayFlow` objects (Ticket #5b, real IPs and
real corrected timestamps from `datasets/TrafficLabelling `) out to a
caller-supplied consumer in wall-clock order, at a configurable speed
multiplier, as micro-batches rather than individual events.

Scope — what this module explicitly does NOT do
-------------------------------------------------------------------------
This engine performs NO scoring, NO database access, and NO WebSocket
work. Those belong to Tickets #7 (StreamingScorer + persistence + fanout)
and #9. Keeping them out is what protects Invariant B (no accidental
model refit happens here — there is no model here at all) and keeps the
engine/consumer boundary clean: the engine's entire contract with the
outside world is the `consumer(batch, meta)` callback and the
`inject()`/`start()`/`stop()`/`set_speed()`/`status()`/`preload()` control
surface below. A future contributor extending this file should find
"score this batch" or "INSERT this batch" unnatural to add here — that
logic lives on the far side of `consumer`.

Threading, not asyncio
-------------------------------------------------------------------------
The reader (`ReplayFlowReader`, stdlib `csv`), the eventual consumer
(sklearn scoring, SQLAlchemy) are all synchronous, blocking code. Bridging
that into an asyncio event loop would mean either running everything in an
executor (which is just a thread pool wearing an asyncio costume) or
accepting that a slow synchronous consumer call stalls the whole event
loop. A plain `threading.Thread` running a tick loop is the simpler,
honest model here: the engine thread ticks on a fixed interval, computes
which flows are due, and calls `consumer(...)` synchronously and
in-thread. If the consumer blocks, the engine visibly falls behind
schedule (see "Backpressure and lag" below) rather than corrupting shared
async state.

Tick-based pacing, absolute targets (no drift)
-------------------------------------------------------------------------
A naive pacer that does `sleep(next_ts - prev_ts)` in a loop accumulates
scheduling error from wakeup jitter and the (non-zero) cost of the
consumer call itself. This engine instead computes, once per `start()`
call (and again on `set_speed()`), a fixed anchor pair
`(anchor_wall, anchor_virtual)` and derives every flow's wall-clock
deadline from that fixed anchor, never from the previous flow:

    wall_target(flow) = anchor_wall + (virtual_time(flow) - anchor_virtual) / speed

`due_index()` below is the pure function that, given the anchor, `speed`,
and the current wall clock, finds how far into the (virtual-time-sorted)
flow list the schedule has advanced. No sleep duration is ever computed
from a delta between two flows.

Within-bucket interpolation — the heart of it (trap T3's mitigation)
-------------------------------------------------------------------------
Measured fact (docs/PHASE5_RECON.md / Ticket #6 brief, Fact A):
friday-morning packs 191,033 rows into only 241 distinct timestamps
(median 629 events/bucket, max 4,017). Pacing directly off `ts` deltas
would fire an entire bucket in one instant and then idle for the multi-
second gap to the next distinct timestamp — a strobe, not a stream, and
it would also blow the peak per-batch throughput budget (Fact B: a
single 4,017-event burst is 2.4x over the measured per-event budget at
that density; batching is what buys back the needed headroom).

`compute_virtual_times()` fixes this by spreading each run of N flows
sharing one timestamp `T_k` evenly across the gap to the next *distinct*
timestamp `T_{k+1}`:

    gap                = T_{k+1} - T_k        (final run: reuse the previous gap)
    virtual_time(i-th) = T_k + (i / N) * gap   for i in 0..N-1

`virtual_time` is a pure pacing quantity, in seconds-since-epoch,
completely separate from `ReplayFlow.ts`.

Critical honesty requirement — `ts` is never mutated
-------------------------------------------------------------------------
`virtual_time` is used ONLY to decide *when* a flow is emitted. The
`ReplayFlow` objects handed to `consumer()` are the exact same, unmodified
objects `ReplayFlowReader` produced — `flow.ts` is always the original
corrected source timestamp. This engine never writes an interpolated time
into `ts` and never presents interpolated ordering as an observed arrival
time; `ReplayFlow.timing_provenance` (Ticket #5b, `backend/models.py`)
already records which timing tier a row came from, and that field is not
touched here either. On Monday (second-resolution, median 9 events per
bucket, per Fact A's companion measurement) interpolation moves events by
a negligible amount and timing stays essentially genuine; on the other,
minute-granularity days the interpolation is real and is exactly what
`timing_provenance == "interpolated_minute_bucket"` already discloses.

Micro-batch emission
-------------------------------------------------------------------------
The consumer signature takes a **list**, never a single flow:

    consumer(batch: list[ReplayFlow], meta: BatchMeta) -> None

This is not an optimization detail — Fact B shows a per-event consumer
(score one row, insert one row) costs 1.78 ms/event against a 0.747
ms/event budget at peak density (2.4x over), while a batched path
(vectorized scoring, `executemany` insert) costs ~0.019 ms/event (~40x
under budget). Emitting micro-batches is also what keeps the eventual
WebSocket fanout (Ticket #9) from sending hundreds of frames per second
to the frontend.

Load once, replay many
-------------------------------------------------------------------------
Fact C: a full read+sort of a capture day costs real time (wednesday:
28.3s / ~380MiB). `preload(day)` reads and cachees a day's sorted flow
list plus its precomputed virtual times exactly once; `start()` always
calls `preload()` first, which is a no-op if that day is already cached.
`stop()` then `start()` on the same day therefore never re-reads the CSV.
Ticket #8's API should call `preload()` at process startup to pay Fact C's
cost once, off the demo's critical path.

Session identity
-------------------------------------------------------------------------
Each `start()` (and each `loop=True` re-iteration) mints a fresh
`replay_session_id` (UUID). This is the value Ticket #7 will write into
`Event.replay_session_id`, which pairs with `Event.source_row_id` under
the Ticket #2 (D4) unique constraint — so replaying the same day again in
a new session is allowed, while re-emitting the same source row twice
*within* one session is a dedup violation downstream, by design.
"""
from __future__ import annotations

import bisect
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from backend.config import BACKEND_SETTINGS
from backend.replay_reader import ReplayFlow, ReplayFlowReader

logger = logging.getLogger(__name__)

#: `BatchMeta.origin` values — a batch is either paced from the scheduled
#: replay stream, or drained from `inject()`'s queue. The two are never
#: mixed within one batch, so `origin` alone (not a per-flow flag on
#: `ReplayFlow`, which this module does not own — see backend/replay_reader.py,
#: Ticket #5b, ACCEPTED) is sufficient for the consumer to "distinguish
#: [injected flows] from replayed traffic" per the ticket brief.
BATCH_ORIGIN_REPLAY = "replay"
BATCH_ORIGIN_INJECTED = "injected"


class ReplayEngineError(RuntimeError):
    """Engine usage errors: `start()` while already running, or an
    `inject()` call that would exceed `replay_injection_queue_max`."""


# ---------------------------------------------------------------------------
# BatchMeta / ReplayStatus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchMeta:
    """Metadata accompanying one emitted micro-batch.

    `lag_seconds` is the engine's lag *as of this batch's emission* (see
    the module docstring, "Backpressure and lag") — Ticket #7 can use it
    to, e.g., skip a debounced recomputation if the engine is already
    behind schedule.
    """

    replay_session_id: uuid.UUID
    day: str
    speed: float
    batch_index: int
    emitted_at: datetime
    lag_seconds: float
    origin: str  # BATCH_ORIGIN_REPLAY | BATCH_ORIGIN_INJECTED


@dataclass(frozen=True)
class ReplayStatus:
    """Point-in-time snapshot returned by `ReplayEngine.status()`.

    `total_for_day` is the full day's flow count as read from disk
    (unaffected by a `limit=` passed to `start()`), so a progress
    percentage against it is honest about whether a `limit`-bounded run
    will ever reach 100% (it correctly will not).

    `emitted_count` counts flows the engine scheduled and handed to the
    consumer — it increments unconditionally, whether or not the consumer
    call succeeded. `consumer_failed_flow_count` is the subset of those
    that were in a batch whose consumer call raised, i.e. flows the
    engine attempted to hand off but the consumer did not successfully
    process. Successfully-processed flows are therefore
    `emitted_count - consumer_failed_flow_count`. `consumer_error_count`
    counts failed *batches*, not flows — the two answer different
    questions (how many failures vs. how many flows were affected) and
    are both kept.
    """

    running: bool
    day: Optional[str]
    speed: Optional[float]
    replay_session_id: Optional[uuid.UUID]
    emitted_count: int
    total_for_day: int
    current_virtual_position: Optional[datetime]
    lag_seconds: float
    batches_emitted: int
    consumer_error_count: int
    consumer_failed_flow_count: int


Consumer = Callable[[list, BatchMeta], None]


# ---------------------------------------------------------------------------
# Pure scheduling functions — no engine state, no I/O, no clock reads.
# Tested directly in tests/test_replay_engine.py without threads.
# ---------------------------------------------------------------------------


def compute_virtual_times(timestamps: Sequence[datetime]) -> list[float]:
    """Pacing-only virtual times (seconds-since-epoch), one per input ts.

    `timestamps` must be sorted non-decreasing (the contract
    `ReplayFlowReader.iter_flows()` already guarantees, P5-7). For a run
    of N entries sharing one timestamp `T_k`, with `T_{k+1}` the next
    *distinct* timestamp:

        gap                = T_{k+1} - T_k   (final run: reuse the previous gap)
        virtual_time(i-th) = T_k + (i / N) * gap,  i in 0..N-1

    Pure and side-effect-free: does not read `ReplayFlow`, does not touch
    a clock, does not mutate its input. This is the function the T3
    "no bursts" mitigation and the honesty requirement both rest on.

    Returns a list the same length as `timestamps`, strictly
    non-decreasing overall (strictly increasing within a run whenever
    gap > 0), with every value in `[T_k, T_{k+1})` for its run.
    """
    n = len(timestamps)
    if n == 0:
        return []
    virtual: list[float] = [0.0] * n
    i = 0
    last_gap = 0.0
    while i < n:
        j = i
        while j < n and timestamps[j] == timestamps[i]:
            j += 1
        run_len = j - i
        t_k = timestamps[i].timestamp()
        if j < n:
            gap = timestamps[j].timestamp() - t_k
            last_gap = gap
        else:
            # Final run in the whole sequence: no next distinct timestamp
            # exists to derive a gap from, so reuse the immediately
            # preceding transition's gap (ticket brief, explicitly).
            gap = last_gap
        for k in range(run_len):
            virtual[i + k] = t_k + (k / run_len) * gap
        i = j
    return virtual


def wall_target(
    virtual_time: float, anchor_wall: float, anchor_virtual: float, speed: float
) -> float:
    """Pure. The fixed-anchor deadline formula from the ticket brief:

        wall_target(flow) = anchor_wall + (virtual_time(flow) - anchor_virtual) / speed

    Computing every deadline from one fixed anchor (never from the
    previous flow's deadline, never by accumulating sleeps) is what
    prevents scheduling drift over a long replay run.
    """
    return anchor_wall + (virtual_time - anchor_virtual) / speed


def due_index(
    virtual_times: Sequence[float],
    pointer: int,
    anchor_wall: float,
    anchor_virtual: float,
    speed: float,
    now_wall: float,
) -> int:
    """Pure. Returns the new pointer (an exclusive upper bound into
    `virtual_times`) such that every flow in `[pointer, result)` has
    `wall_target(...) <= now_wall` under the given fixed anchor.

    Implemented as a bisection on `now` converted into virtual time
    (`now_virtual = anchor_virtual + (now_wall - anchor_wall) * speed`)
    rather than a linear scan, since `virtual_times` is sorted
    non-decreasing by construction (`compute_virtual_times()`).
    """
    if pointer >= len(virtual_times):
        return pointer
    now_virtual = anchor_virtual + (now_wall - anchor_wall) * speed
    return bisect.bisect_right(virtual_times, now_virtual, lo=pointer)


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------


class ReplayEngine:
    """Threading-based pacer for `ReplayFlow` streams. See module docstring.

    Parameters
    ----------
    consumer :
        `consumer(batch, meta)`, called synchronously on the engine's
        background thread for every emitted micro-batch (scheduled or
        injected — distinguished via `meta.origin`). A raising consumer
        does not kill the engine thread: the exception is logged, the
        failing batch counted in both `status().consumer_error_count`
        (batches) and `status().consumer_failed_flow_count` (flows), and
        replay continues with the next tick (see `_emit_batch`) — a
        single bad batch should not end a live demo, and retry/backoff
        policy belongs to the consumer (Ticket #7), not this engine.
    reader :
        Defaults to a new `ReplayFlowReader()`. Overridable for tests
        (a fake/spy reader) without touching real files.
    tick_interval, lag_warning_threshold, injection_queue_max,
    thread_join_timeout, max_batch_size :
        Optional overrides; fall back to the matching `BACKEND_SETTINGS`
        field when `None` (CLAUDE.md optional-override convention).
        `max_batch_size` caps how many flows a single scheduled batch may
        contain; when more flows are due in a tick than the cap, the
        engine emits exactly the cap and carries the remainder forward to
        the next tick (see `_tick_once`) — flows are never dropped.
    clock :
        Defaults to `time.monotonic`. Tests inject a fake, manually
        advanced clock and drive `_tick_once()` directly instead of the
        real background thread, per the ticket's determinism requirement.
    """

    def __init__(
        self,
        consumer: Consumer,
        reader: Optional[ReplayFlowReader] = None,
        tick_interval: Optional[float] = None,
        lag_warning_threshold: Optional[float] = None,
        injection_queue_max: Optional[int] = None,
        thread_join_timeout: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
        max_batch_size: Optional[int] = None,
    ) -> None:
        self._consumer = consumer
        self._reader = reader if reader is not None else ReplayFlowReader()
        self._tick_interval = (
            tick_interval
            if tick_interval is not None
            else BACKEND_SETTINGS.replay_tick_interval_sec
        )
        self._lag_warning_threshold = (
            lag_warning_threshold
            if lag_warning_threshold is not None
            else BACKEND_SETTINGS.replay_lag_warning_threshold_sec
        )
        self._injection_queue_max = (
            injection_queue_max
            if injection_queue_max is not None
            else BACKEND_SETTINGS.replay_injection_queue_max
        )
        self._thread_join_timeout = (
            thread_join_timeout
            if thread_join_timeout is not None
            else BACKEND_SETTINGS.replay_thread_join_timeout_sec
        )
        self._max_batch_size = (
            max_batch_size
            if max_batch_size is not None
            else BACKEND_SETTINGS.replay_max_batch_size
        )
        self._clock = clock if clock is not None else time.monotonic

        # Load-once cache: day -> (sorted flows, precomputed virtual times).
        self._cache: dict[str, tuple[list[ReplayFlow], list[float]]] = {}
        self._cache_lock = threading.Lock()

        # Engine state, guarded by _state_lock. RLock because status()/
        # set_speed() may be called from the engine's own thread (e.g. a
        # future consumer calling back in) as well as external threads.
        self._state_lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._flows: list[ReplayFlow] = []
        self._virtual_times: list[float] = []
        self._pointer = 0
        self._day: Optional[str] = None
        self._speed: float = 1.0
        self._anchor_wall = 0.0
        self._anchor_virtual = 0.0
        self._session_id: Optional[uuid.UUID] = None
        self._loop_flag = False
        self._running = False
        self._emitted_count = 0
        self._total_for_day = 0
        self._batches_emitted = 0
        self._lag_seconds = 0.0
        self._lag_warned = False
        self._consumer_error_count = 0
        self._consumer_failed_flow_count = 0

        self._injection_lock = threading.Lock()
        self._injection_buffer: list[ReplayFlow] = []

    # ------------------------------------------------------------------
    # Cache / preload
    # ------------------------------------------------------------------

    def preload(self, day: Optional[str] = None) -> None:
        """Read and cache `day`'s flows (full, unlimited) plus their
        precomputed virtual times, if not already cached. A no-op if
        already cached — this is what makes `stop()` then `start()`, and
        `start()` under Ticket #8's API warmup, avoid re-reading the CSV
        (Fact C).
        """
        resolved_day = self._resolve_day(day)
        with self._cache_lock:
            if resolved_day in self._cache:
                return
            flows = list(self._reader.iter_flows(day=resolved_day))
            virtual_times = compute_virtual_times([f.ts for f in flows])
            self._cache[resolved_day] = (flows, virtual_times)

    @staticmethod
    def _resolve_day(day: Optional[str]) -> str:
        return day if day is not None else BACKEND_SETTINGS.replay_default_dataset_day

    # ------------------------------------------------------------------
    # Control surface
    # ------------------------------------------------------------------

    def start(
        self,
        day: Optional[str] = None,
        speed: Optional[float] = None,
        start_at: Optional[datetime | str] = None,
        limit: Optional[int] = None,
        loop: bool = False,
    ) -> uuid.UUID:
        """Start replaying `day` at `speed`, spawning the engine thread.

        `day`/`speed` default to `BACKEND_SETTINGS.replay_default_dataset_day`
        / `.replay_speed` when `None` (CLAUDE.md optional-override
        convention). `start_at` (a `datetime`, or an `"HH:MM"` string
        combined with the day file's own calendar date) skips into the
        capture — e.g. friday-morning's first real attack lands at 09:34,
        about 1.5 minutes into a 20x replay from the top. `limit` caps how
        many flows (from `start_at` onward) this run will ever emit.

        Calling `start()` while already running is a hard error
        (`ReplayEngineError`), deliberately not a no-op: a caller that
        wanted a different day/speed must never be able to mistake a
        silently-ignored `start()` call for one that took effect. Call
        `stop()` first.

        Returns the freshly minted `replay_session_id`.
        """
        with self._state_lock:
            if self._running:
                raise ReplayEngineError(
                    "start() called while the engine is already running "
                    "(day={!r}, session={}). start() is a hard error here, "
                    "not a no-op, so a caller can never mistake an ignored "
                    "day/speed change for one that took effect — call "
                    "stop() first.".format(self._day, self._session_id)
                )

            resolved_day = self._resolve_day(day)
            resolved_speed = (
                speed if speed is not None else BACKEND_SETTINGS.replay_speed
            )
            if resolved_speed <= 0:
                raise ValueError(f"speed must be > 0, got {resolved_speed}")

            self.preload(resolved_day)
            full_flows, full_virtual = self._cache[resolved_day]

            start_index = 0
            if start_at is not None and full_flows:
                start_dt = self._coerce_start_at(start_at, full_flows[0].ts)
                start_index = bisect.bisect_left(
                    full_flows, start_dt, key=lambda f: f.ts
                )

            end_index = len(full_flows)
            if limit is not None:
                end_index = min(end_index, start_index + max(limit, 0))

            self._flows = full_flows[start_index:end_index]
            self._virtual_times = full_virtual[start_index:end_index]
            self._total_for_day = len(full_flows)
            self._pointer = 0
            self._day = resolved_day
            self._speed = resolved_speed
            self._loop_flag = loop
            self._session_id = uuid.uuid4()
            self._emitted_count = 0
            self._batches_emitted = 0
            self._lag_seconds = 0.0
            self._lag_warned = False
            self._consumer_error_count = 0
            self._consumer_failed_flow_count = 0

            with self._injection_lock:
                self._injection_buffer.clear()

            now = self._clock()
            self._anchor_wall = now
            self._anchor_virtual = self._virtual_times[0] if self._virtual_times else 0.0

            self._stop_event.clear()
            self._running = True
            session_id = self._session_id
            thread = threading.Thread(
                target=self._run_loop, name="aegis-replay-engine", daemon=True
            )
            self._thread = thread

        thread.start()
        return session_id

    def stop(self) -> None:
        """Idempotent. Signals the engine thread to stop and joins it with
        a bounded timeout (`replay_thread_join_timeout_sec`). Safe to call
        when not running (returns immediately) and safe to call twice.
        """
        with self._state_lock:
            thread = self._thread
            if thread is None:
                self._running = False
                return
            self._stop_event.set()

        thread.join(timeout=self._thread_join_timeout)
        if thread.is_alive():  # pragma: no cover - only under a wedged consumer
            logger.warning(
                "replay engine thread did not join within %.1fs "
                "(a consumer call may be blocked)",
                self._thread_join_timeout,
            )

        with self._state_lock:
            self._running = False
            self._thread = None

    def set_speed(self, speed: float) -> None:
        """Change replay speed mid-run, re-anchoring so the stream neither
        jumps forward nor stalls (a demo action that must look smooth).

        Re-anchoring works by converting "now" into its equivalent virtual
        time under the *old* anchor/speed, then making that pair the new
        anchor: every flow's `wall_target()` computed after this call is
        continuous with the schedule computed before it.
        """
        if speed <= 0:
            raise ValueError(f"speed must be > 0, got {speed}")
        with self._state_lock:
            now = self._clock()
            current_virtual = self._anchor_virtual + (now - self._anchor_wall) * self._speed
            self._anchor_wall = now
            self._anchor_virtual = current_virtual
            self._speed = speed

    def inject(self, flows: list[ReplayFlow]) -> None:
        """Queue `flows` for emission on the engine's next tick, ahead of
        the scheduled stream, as their own batch tagged
        `origin=BATCH_ORIGIN_INJECTED` (see the module-level constants) so
        the consumer can distinguish them from replayed traffic. Ticket
        #13 wires an HTTP endpoint to this method; this engine owns the
        queuing/emission mechanism so #13 never reaches into internals.

        Raises `ReplayEngineError` rather than silently dropping flows if
        the pending queue would exceed `replay_injection_queue_max`.
        """
        if not flows:
            return
        with self._injection_lock:
            pending = len(self._injection_buffer)
            if pending + len(flows) > self._injection_queue_max:
                raise ReplayEngineError(
                    f"inject() would exceed the injection queue capacity "
                    f"({self._injection_queue_max}): {pending} already "
                    f"pending + {len(flows)} new."
                )
            self._injection_buffer.extend(flows)

    def status(self) -> ReplayStatus:
        """Point-in-time snapshot. Thread-safe, cheap, non-blocking."""
        with self._state_lock:
            current_virtual: Optional[datetime] = None
            if self._virtual_times:
                idx = min(self._pointer, len(self._virtual_times) - 1)
                current_virtual = datetime.fromtimestamp(
                    self._virtual_times[idx], tz=timezone.utc
                )
            return ReplayStatus(
                running=self._running,
                day=self._day,
                speed=self._speed if self._day is not None else None,
                replay_session_id=self._session_id,
                emitted_count=self._emitted_count,
                total_for_day=self._total_for_day,
                current_virtual_position=current_virtual,
                lag_seconds=self._lag_seconds,
                batches_emitted=self._batches_emitted,
                consumer_error_count=self._consumer_error_count,
                consumer_failed_flow_count=self._consumer_failed_flow_count,
            )

    # ------------------------------------------------------------------
    # start_at coercion
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_start_at(start_at: datetime | str, reference_ts: datetime) -> datetime:
        """A `datetime` is used as-is (assumed/forced UTC if naive). An
        `"HH:MM"` string is combined with `reference_ts`'s calendar date
        (the day file's own date — every row in one capture file shares a
        single date) to produce a full `datetime`.
        """
        if isinstance(start_at, datetime):
            return start_at if start_at.tzinfo else start_at.replace(tzinfo=timezone.utc)
        hour_str, sep, minute_str = start_at.partition(":")
        if not sep:
            raise ValueError(f"start_at string must be 'HH:MM', got {start_at!r}")
        try:
            hour = int(hour_str)
            minute = int(minute_str)
        except ValueError as exc:
            raise ValueError(f"start_at string must be 'HH:MM', got {start_at!r}") from exc
        return datetime(
            reference_ts.year,
            reference_ts.month,
            reference_ts.day,
            hour,
            minute,
            tzinfo=timezone.utc,
        )

    # ------------------------------------------------------------------
    # Engine thread body — pure scheduling logic lives in due_index()/
    # wall_target()/compute_virtual_times() above; this is the glue.
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._flows:
                # Nothing scheduled for this run (e.g. limit=0, or
                # start_at past the end of the day). Stop rather than
                # spin — looping an empty schedule would busy-loop.
                with self._state_lock:
                    self._running = False
                return

            now = self._clock()
            self._tick_once(now)

            with self._state_lock:
                exhausted = self._pointer >= len(self._flows)

            if exhausted:
                if self._loop_flag:
                    self._start_new_loop_iteration()
                    continue
                with self._state_lock:
                    self._running = False
                return

            self._stop_event.wait(self._tick_interval)

    def _start_new_loop_iteration(self) -> None:
        """On exhausting the day with `loop=True`: mint a NEW
        `replay_session_id` before restarting, so every looped row gets a
        fresh (session_id, source_row_id) pair — otherwise the second
        pass through the same file would collide with the Ticket #2 (D4)
        unique constraint downstream.
        """
        with self._state_lock:
            self._session_id = uuid.uuid4()
            self._pointer = 0
            now = self._clock()
            self._anchor_wall = now
            self._anchor_virtual = self._virtual_times[0] if self._virtual_times else 0.0

    def _tick_once(self, now: float) -> None:
        """One scheduling step: drain any injected flows first (their own
        batch, ahead of the scheduled stream), then emit whatever
        scheduled flows are due. Exposed as a separate method (rather than
        inlined in `_run_loop`) specifically so tests can drive it
        directly with a fake clock, with no real thread and no real
        sleeping involved (determinism requirement).
        """
        injected = self._drain_injection_buffer()
        if injected:
            self._emit_batch(injected, origin=BATCH_ORIGIN_INJECTED)

        with self._state_lock:
            anchor_wall = self._anchor_wall
            anchor_virtual = self._anchor_virtual
            speed = self._speed
            pointer = self._pointer

        idx = due_index(
            self._virtual_times, pointer, anchor_wall, anchor_virtual, speed, now
        )
        if idx <= pointer:
            return

        # Cap emission to at most `_max_batch_size` flows per tick (MEDIUM-1
        # fix). If more flows are due than the cap allows, emit exactly the
        # cap and leave the remainder at `capped_idx` — it is the
        # chronologically-next contiguous slice, so it is picked up
        # (in order) on a subsequent tick without any flow being dropped.
        # Computing `lag` against `capped_idx - 1` (rather than the true
        # due index `idx - 1`) is deliberate: it makes the schedule
        # slippage caused by capping show up honestly in
        # status().lag_seconds instead of being hidden.
        capped_idx = min(idx, pointer + self._max_batch_size)

        batch = self._flows[pointer:capped_idx]
        last_target = wall_target(
            self._virtual_times[capped_idx - 1], anchor_wall, anchor_virtual, speed
        )
        lag = max(0.0, now - last_target)
        self._update_lag(lag)
        self._emit_batch(batch, origin=BATCH_ORIGIN_REPLAY)
        with self._state_lock:
            self._pointer = capped_idx

    def _drain_injection_buffer(self) -> list[ReplayFlow]:
        with self._injection_lock:
            if not self._injection_buffer:
                return []
            drained = self._injection_buffer
            self._injection_buffer = []
            return drained

    def _emit_batch(self, batch: list[ReplayFlow], origin: str) -> None:
        with self._state_lock:
            self._batches_emitted += 1
            meta = BatchMeta(
                replay_session_id=self._session_id,
                day=self._day,
                speed=self._speed,
                batch_index=self._batches_emitted,
                emitted_at=datetime.now(timezone.utc),
                lag_seconds=self._lag_seconds,
                origin=origin,
            )

        try:
            self._consumer(batch, meta)
        except Exception:
            # A raising consumer must not kill the engine thread silently
            # (ticket requirement): log, count, and keep replaying. Retry/
            # backoff policy is the consumer's (Ticket #7's) concern, not
            # this engine's — a single bad batch should not end a live
            # demo.
            logger.exception(
                "replay engine consumer raised on batch %d (origin=%s, "
                "day=%s, %d flows); continuing",
                meta.batch_index,
                origin,
                meta.day,
                len(batch),
            )
            with self._state_lock:
                self._consumer_error_count += 1
                self._consumer_failed_flow_count += len(batch)

        with self._state_lock:
            self._emitted_count += len(batch)

    def _update_lag(self, lag: float) -> None:
        with self._state_lock:
            self._lag_seconds = lag
            if lag > self._lag_warning_threshold:
                if not self._lag_warned:
                    logger.warning(
                        "replay engine lag %.2fs exceeds threshold %.2fs "
                        "(day=%s speed=%s) — consumer is falling behind "
                        "the schedule",
                        lag,
                        self._lag_warning_threshold,
                        self._day,
                        self._speed,
                    )
                    self._lag_warned = True
            else:
                self._lag_warned = False
