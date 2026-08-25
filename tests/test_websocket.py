"""
tests/test_websocket.py — Phase 5 Ticket #9: `WS /ws/stream`
(backend/routes.py's `ws_stream`, backend/ws_broadcaster.py).

The default suite needs neither Postgres nor a real replay
(docs/PHASE5_TICKET9_PLAN.md section 7): a minimal FastAPI app is built
here, wired with a REAL `WebSocketBroadcaster` and a fake `AppRuntime`
(`engine=None`, `pipeline=None`, `scorer=None`) via a tiny test-only
lifespan that plays the one role `backend.main.lifespan` normally plays
for the broadcaster -- capturing the running event loop and calling
`broadcaster.set_loop()` (decision D9-1). This is deliberately NOT
`backend.main.create_app()` + `dependency_overrides`: that path would
either skip the real lifespan (leaving the broadcaster's loop unset) or
require a real `StreamingScorer` artifact, neither of which this
transport-only ticket needs.

`WS /ws/stream` itself needs no DB and no scorer -- it only reads
`runtime.engine.status()` (for the hello frame) and drives
`runtime.broadcaster`, so `engine=None` (mirroring "scorer failed to
load, API still starts" from Ticket #8) is a perfectly realistic fixture,
not a shortcut.

The one test that matters most: `test_publish_from_background_thread_
reaches_client` drives `broadcaster.publish()` from an ACTUAL
`threading.Thread`, not the test thread -- proving the `call_soon_
threadsafe` hop (D9-1) actually works, which is the whole point of this
ticket's hard part.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes import router
from backend.runtime import AppRuntime
from backend.ws_broadcaster import WebSocketBroadcaster, _ClientState

# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def make_test_app(
    *,
    broadcaster: WebSocketBroadcaster | None = None,
    engine=None,
) -> tuple[FastAPI, AppRuntime]:
    """A minimal app carrying only `WS /ws/stream`, wired to a fake
    `AppRuntime` (no scorer, no pipeline, optionally no engine) and a real
    `WebSocketBroadcaster`. The test-only lifespan below plays exactly the
    one role `backend.main.lifespan` plays for the broadcaster: capturing
    `asyncio.get_running_loop()` and calling `set_loop()` (D9-1) -- without
    it the broadcaster stays in its safe no-op mode and every test here
    would fail by construction, which is the point: this proves the real
    wiring, not a shortcut around it.
    """
    broadcaster = broadcaster if broadcaster is not None else WebSocketBroadcaster()
    runtime = AppRuntime(
        scorer=None,
        pipeline=None,
        engine=engine,
        scorer_load_error="test fixture: no scorer built",
        started_at=datetime.now(timezone.utc),
        broadcaster=broadcaster,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broadcaster.set_loop(asyncio.get_running_loop())
        app.state.runtime = runtime
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app, runtime


class _FakeEngine:
    """Just enough of `ReplayEngine` for the hello frame's `status()`
    call -- `ws_stream` never touches anything else on the engine."""

    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


def _make_status(**overrides):
    from backend.replay_engine import ReplayStatus

    defaults = dict(
        running=False,
        day=None,
        speed=None,
        replay_session_id=None,
        emitted_count=0,
        total_for_day=0,
        current_virtual_position=None,
        lag_seconds=0.0,
        batches_emitted=0,
        consumer_error_count=0,
        consumer_failed_flow_count=0,
    )
    defaults.update(overrides)
    return ReplayStatus(**defaults)


# ---------------------------------------------------------------------------
# Hello frame + basic publish
# ---------------------------------------------------------------------------


def test_hello_frame_on_connect():
    """A freshly-connected client gets an immediate `{"type": "hello", ...}`
    frame carrying `runtime.engine.status()`, before any event is
    published -- D9-3."""
    engine = _FakeEngine(_make_status(running=True, day="friday-morning", speed=20.0))
    app, _runtime = make_test_app(engine=engine)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["data"]["running"] is True
            assert hello["data"]["day"] == "friday-morning"
            assert hello["data"]["speed"] == 20.0


def test_hello_frame_when_no_engine():
    """`engine=None` (scorer never loaded) must still accept the
    connection and send an all-idle hello frame, not fail the connect."""
    app, _runtime = make_test_app(engine=None)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["data"]["running"] is False


def test_publish_reaches_connected_client():
    """A published envelope reaches a connected client, unchanged."""
    app, runtime = make_test_app()
    envelope = {"type": "event", "data": {"id": 1, "source_ip": "192.168.10.5"}}
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()  # hello
            runtime.broadcaster.publish(envelope)
            received = ws.receive_json()
            assert received == envelope


def test_publish_from_background_thread_reaches_client():
    """THE D9-1 PROOF: `publish()` driven from an actual `threading.Thread`
    (mirroring `ReplayEngine`'s background thread, not the test/event-loop
    thread) must still deliver to a connected client via the
    `call_soon_threadsafe` hop, with no race or corruption."""
    app, runtime = make_test_app()
    envelope = {"type": "event", "data": {"id": 2, "source_ip": "192.168.10.9"}}
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()  # hello

            def _publish_from_thread():
                runtime.broadcaster.publish(envelope)

            t = threading.Thread(target=_publish_from_thread)
            t.start()
            t.join(timeout=5.0)
            assert not t.is_alive()

            received = ws.receive_json()
            assert received == envelope


def test_publish_multiple_envelopes_from_multiple_threads():
    """Several background threads publishing concurrently must all land,
    in some order, with no loss and no corruption -- a stronger version of
    the single-thread D9-1 proof above."""
    app, runtime = make_test_app()
    envelopes = [{"type": "event", "data": {"id": i}} for i in range(10)]
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()  # hello

            threads = [
                threading.Thread(target=runtime.broadcaster.publish, args=(env,))
                for env in envelopes
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
                assert not t.is_alive()

            received_ids = {ws.receive_json()["data"]["id"] for _ in envelopes}
            assert received_ids == {env["data"]["id"] for env in envelopes}


# ---------------------------------------------------------------------------
# publish() never blocks/raises with no client and/or no loop
# ---------------------------------------------------------------------------


def test_publish_with_no_client_connected_is_a_prompt_noop():
    broadcaster = WebSocketBroadcaster()
    app, runtime = make_test_app(broadcaster=broadcaster)
    # Enter the lifespan (sets the loop) without ever connecting a client.
    with TestClient(app):
        runtime.broadcaster.publish({"type": "event", "data": {}})
        # No exception, no hang -- if we get here, it passed.


def test_publish_with_no_loop_set_is_a_counted_noop():
    """`IngestPipeline` driven outside the API process (as Ticket #7's own
    tests do) never calls `set_loop()` at all. `publish()` must degrade to
    a counted no-op, never raise -- D9-1's explicit guard."""
    broadcaster = WebSocketBroadcaster()
    before = broadcaster.stats().publish_no_loop_count
    broadcaster.publish({"type": "event", "data": {}})
    broadcaster.publish({"type": "alert", "data": {}})
    stats = broadcaster.stats()
    assert stats.publish_no_loop_count == before + 2
    assert stats.connected_clients == 0


# ---------------------------------------------------------------------------
# Backpressure (D9-2)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Enough of `starlette.websockets.WebSocket` for broadcaster-internal
    tests that never go through a real ASGI connection."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        return None


class _NeverRespondingFakeWebSocket(_FakeWebSocket):
    """A `send_json` that never returns -- simulates a client whose
    connection is technically open but whose OS receive buffer is full
    and never drained, so the server-side write hangs. Real TestClient
    websocket connections don't reproduce this (their in-memory transport
    never applies real backpressure), which is why this level is tested
    with a fake rather than a real socket."""

    async def send_json(self, data) -> None:
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_slow_client_does_not_block_publish_or_a_second_client():
    """A client whose writer task is stuck on a hung send must not delay
    `publish()` (only ever `call_soon_threadsafe`, D9-1) nor a second,
    actively-draining client's delivery. The stuck writer times out after
    `send_timeout_sec` and ends; further envelopes for that client then
    queue up and overflow, dropping the oldest (D9-2) -- proving the slow
    client affects only itself.
    """
    broadcaster = WebSocketBroadcaster(queue_max=3, send_timeout_sec=0.2)
    broadcaster.set_loop(asyncio.get_running_loop())

    slow_ws = _NeverRespondingFakeWebSocket()
    fast_ws = _FakeWebSocket()
    slow_client = await broadcaster.register(slow_ws, {"running": False})
    fast_client = await broadcaster.register(fast_ws, {"running": False})

    for i in range(10):
        start = asyncio.get_running_loop().time()
        broadcaster.publish({"type": "event", "data": {"n": i}})
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < 0.1, "publish() must return immediately, never wait on a client"
        # Yield to the loop so the fast client's writer task can drain this
        # envelope before the next one is published -- otherwise all 10
        # would enqueue within one loop turn, before any writer task has
        # run at all, overflowing BOTH clients' queues regardless of
        # speed and proving nothing about the slow client specifically.
        await asyncio.sleep(0.02)

    # The slow client's writer hangs on its first send until
    # send_timeout_sec, then ends, after which further enqueues for it
    # overflow -- give it time to reach that state too.
    await asyncio.sleep(0.5)

    fast_events = [m for m in fast_ws.sent if m["type"] == "event"]
    assert [m["data"]["n"] for m in fast_events] == list(range(10))
    assert slow_client.dropped >= 1
    assert fast_client.dropped == 0

    await broadcaster.unregister(slow_client)
    await broadcaster.unregister(fast_client)


@pytest.mark.asyncio
async def test_queue_overflow_drops_oldest_and_counts():
    """Overflowing a client's queue drops the OLDEST envelope and
    increments its `dropped` counter -- never blocks, never raises, never
    silent. Exercises `_dispatch`/`_enqueue` directly (bypassing the
    writer task, which would otherwise race the test by draining the
    queue as fast as envelopes are enqueued) so the overflow condition is
    deterministic.
    """
    broadcaster = WebSocketBroadcaster(queue_max=2, send_timeout_sec=1.0)
    client = _ClientState(
        client_id=1,
        websocket=_FakeWebSocket(),
        queue=asyncio.Queue(maxsize=2),
    )
    broadcaster._clients[1] = client

    broadcaster._dispatch({"type": "event", "data": {"n": 1}})
    broadcaster._dispatch({"type": "event", "data": {"n": 2}})
    broadcaster._dispatch({"type": "event", "data": {"n": 3}})  # overflow: drop n=1

    assert client.dropped == 1
    assert client.queue.qsize() == 2
    remaining = [client.queue.get_nowait()["data"]["n"], client.queue.get_nowait()["data"]["n"]]
    assert remaining == [2, 3]

    stats = broadcaster.stats()
    assert stats.total_dropped == 1


# ---------------------------------------------------------------------------
# Disconnect lifecycle (D9-3)
# ---------------------------------------------------------------------------


def test_disconnect_removes_client_second_client_keeps_receiving():
    app, runtime = make_test_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws1:
            ws1.receive_json()  # hello
            assert runtime.broadcaster.stats().connected_clients == 1

        # ws1 closed on context exit; give the server a moment to notice.
        import time

        time.sleep(0.2)
        assert runtime.broadcaster.stats().connected_clients == 0

        with client.websocket_connect("/ws/stream") as ws2:
            ws2.receive_json()  # hello
            assert runtime.broadcaster.stats().connected_clients == 1
            envelope = {"type": "alert", "data": {"id": 99}}
            runtime.broadcaster.publish(envelope)
            assert ws2.receive_json() == envelope


# ---------------------------------------------------------------------------
# Payload shapes unchanged (guard against accidental redesign)
# ---------------------------------------------------------------------------


def test_event_alert_cii_payload_shapes_pass_through_unchanged():
    """This transport must not add, remove, or rename any key inside an
    `event`/`alert`/`cii` envelope's `data` -- it only wraps a hello frame
    around what `backend.ingest` already produces (docs/
    PHASE5_TICKET9_PLAN.md section 5)."""
    app, runtime = make_test_app()
    event_envelope = {
        "type": "event",
        "data": {
            "id": 1,
            "ts": "2017-07-07T09:00:00+00:00",
            "observed_at": "2017-07-07T09:00:00+00:00",
            "source_ip": "192.168.10.5",
            "destination_ip": "192.168.10.50",
            "source_asset": "Workstation_1",
            "destination_asset": "File_Server",
            "protocol": "TCP",
            "bytes": 1500,
            "packets": 10,
            "duration_sec": 0.5,
            "raw_score": -0.12,
            "calibrated_score": 0.83,
            "is_anomaly": True,
            "tripwire_fired": False,
            "confidence": 0.83,
            "replay_session_id": "00000000-0000-0000-0000-000000000000",
            "batch_index": 0,
        },
    }
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()  # hello
            runtime.broadcaster.publish(event_envelope)
            received = ws.receive_json()
            assert received == event_envelope
            assert set(received["data"].keys()) == set(event_envelope["data"].keys())
