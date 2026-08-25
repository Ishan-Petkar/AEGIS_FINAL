"""
backend/ws_broadcaster.py — Phase 5, Ticket #9: the real `WS /ws/stream`
transport.

`backend/ingest.py` defines the `Broadcaster` Protocol
(`publish(envelope) -> None`) and two throwaway implementations
(`NullBroadcaster`, `CollectingBroadcaster`) but deliberately does not
import this module — see that Protocol's docstring on why: the WS layer
must not create an ingest <-> transport import cycle. `WebSocketBroadcaster`
here is the real thing `backend.runtime.build_runtime()` wires in once
`WS /ws/stream` exists.

The hard part (D9-1)
---------------------
`IngestPipeline._safe_publish()` calls `broadcaster.publish(envelope)`
**synchronously, from `ReplayEngine`'s background `threading.Thread`**.
FastAPI/Starlette `WebSocket` objects belong to the asyncio event loop;
touching one from another thread is a race, not just a style nit.

The fix: the event loop is captured exactly ONCE, in `backend.main`'s
lifespan (`asyncio.get_running_loop()`), and handed to this broadcaster via
`set_loop()`. `publish()` itself never touches a socket and never awaits —
it only calls `loop.call_soon_threadsafe(self._dispatch, envelope)`, which
is documented by the stdlib as safe to call from any thread, and returns
immediately. `_dispatch` (which does the actual per-client fan-out) always
runs ON the loop thread, scheduled by `call_soon_threadsafe`, so it may
freely touch client queues without a lock.

If no loop has been set (e.g. `IngestPipeline` driven directly in a test,
or by a process that isn't running this API's lifespan — Ticket #7's own
tests do exactly this), `publish()` degrades to a counted no-op rather
than raising. `_safe_publish` already wraps every `publish()` call in a
`try/except`, but D9-1 explicitly requires this class to never need that
safety net for the "no loop" case — it is an expected, steady-state
condition, not an error.

Backpressure (D9-2)
--------------------
Each connected client gets its own bounded `asyncio.Queue`
(`BACKEND_SETTINGS.ws_client_queue_max`). `_dispatch` fans an envelope out
to every client's queue; on overflow it drops the OLDEST queued envelope
for that client (not the new one — a live view wants the freshest state,
and dropping the incoming envelope instead would make a saturated client
fall permanently behind) and increments that client's `dropped` counter.
One writer task per connection drains its own queue and calls
`WebSocket.send_json`, bounded by `BACKEND_SETTINGS.ws_send_timeout_sec` so
a single stuck socket cannot pin its writer task (and only its own queue)
forever. A slow or dead client therefore affects only itself; it can never
become the replay engine's rate limiter.

Connection lifecycle (D9-3)
----------------------------
`register()` accepts the socket, creates its queue + writer task, and
sends an immediate `{"type": "hello", "data": <status>}` frame so a
freshly-connected client can render state before the next event arrives.
`unregister()` (called from the route's `finally`) removes the client and
cancels its writer task. Nothing here can propagate one client's exception
to another — `_dispatch` swallows a queue-full per-client (that is normal
operation, not an error) and the writer task's own `except` scopes failures
to that one connection.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import WebSocket

from backend.config import BACKEND_SETTINGS

logger = logging.getLogger(__name__)

#: Envelope type for the one payload this ticket is allowed to add (see
#: docs/PHASE5_TICKET9_PLAN.md section 5) — kept out of backend.ingest's
#: ENVELOPE_* constants because ingest never emits it; only this transport
#: layer does, once per connection.
ENVELOPE_HELLO = "hello"

_client_ids = itertools.count()


@dataclass
class _ClientState:
    """Per-connection state: the bounded queue, its writer task, and the
    dropped-envelope counter D9-2 requires to be counted, never silent."""

    client_id: int
    websocket: WebSocket
    queue: "asyncio.Queue[dict[str, Any]]"
    writer_task: Optional["asyncio.Task[None]"] = None
    dropped: int = 0
    sent: int = 0


@dataclass
class BroadcasterStats:
    """Snapshot of `WebSocketBroadcaster`'s counters, mirroring the
    read-only-snapshot style of `backend.ingest.IngestStats`."""

    connected_clients: int
    total_sent: int
    total_dropped: int
    publish_no_loop_count: int


class WebSocketBroadcaster:
    """The real `Broadcaster` (structural match to `backend.ingest.Broadcaster`
    — no import needed, see module docstring) backing `WS /ws/stream`.

    Constructed once by `backend.runtime.build_runtime()` and stored on
    `AppRuntime`; `backend.main`'s lifespan calls `set_loop()` after the
    ASGI server's event loop exists, and `backend/routes.py`'s
    `ws_stream` endpoint calls `register()`/`unregister()` per connection.
    """

    def __init__(
        self,
        queue_max: int | None = None,
        send_timeout_sec: float | None = None,
    ) -> None:
        self._queue_max = queue_max if queue_max is not None else BACKEND_SETTINGS.ws_client_queue_max
        self._send_timeout_sec = (
            send_timeout_sec if send_timeout_sec is not None else BACKEND_SETTINGS.ws_send_timeout_sec
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: dict[int, _ClientState] = {}
        self._total_sent = 0
        self._total_dropped = 0
        self._publish_no_loop_count = 0

    # ------------------------------------------------------------------
    # Loop wiring (D9-1)
    # ------------------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from `backend.main`'s lifespan, on the loop thread,
        after `asyncio.get_running_loop()`. Everything below this point
        assumes `self._loop` is either unset (no-op mode) or the one true
        loop this process's WebSocket connections live on."""
        self._loop = loop

    # ------------------------------------------------------------------
    # Broadcaster protocol — called synchronously from the ReplayEngine's
    # background thread via IngestPipeline._safe_publish(). MUST NOT
    # block, await, or raise (D9-1).
    # ------------------------------------------------------------------

    def publish(self, envelope: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            # Expected steady state outside the API process (e.g. Ticket
            # #7's own tests drive IngestPipeline with no loop at all) —
            # a counted no-op, never a crash.
            self._publish_no_loop_count += 1
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, envelope)
        except RuntimeError:
            # Loop already closed (e.g. shutdown racing a final publish).
            self._publish_no_loop_count += 1

    # ------------------------------------------------------------------
    # Runs ON the loop thread (scheduled via call_soon_threadsafe), so it
    # may touch client state directly without a lock.
    # ------------------------------------------------------------------

    def _dispatch(self, envelope: dict[str, Any]) -> None:
        for client in list(self._clients.values()):
            self._enqueue(client, envelope)

    def _enqueue(self, client: _ClientState, envelope: dict[str, Any]) -> None:
        try:
            client.queue.put_nowait(envelope)
            return
        except asyncio.QueueFull:
            pass
        # Drop the OLDEST queued envelope, not the new one (D9-2) — a live
        # view wants the freshest state once it's behind.
        try:
            client.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        client.dropped += 1
        self._total_dropped += 1
        logger.warning(
            "ws_broadcaster: client %d queue overflow, dropped oldest "
            "envelope (dropped=%d total)",
            client.client_id,
            client.dropped,
        )
        try:
            client.queue.put_nowait(envelope)
        except asyncio.QueueFull:  # pragma: no cover - queue_max >= 1 guards this
            pass

    # ------------------------------------------------------------------
    # Connection lifecycle (D9-3) — called from backend/routes.py's
    # ws_stream endpoint, on the loop thread (it's a native coroutine).
    # ------------------------------------------------------------------

    async def register(self, websocket: WebSocket, hello_data: dict[str, Any]) -> _ClientState:
        """Accept the connection, create its queue + writer task, and send
        the immediate hello frame. Returns the `_ClientState` the caller
        must pass to `unregister()`."""
        await websocket.accept()
        client = _ClientState(
            client_id=next(_client_ids),
            websocket=websocket,
            queue=asyncio.Queue(maxsize=self._queue_max),
        )
        self._clients[client.client_id] = client
        client.writer_task = asyncio.create_task(self._writer_loop(client))
        try:
            await asyncio.wait_for(
                websocket.send_json({"type": ENVELOPE_HELLO, "data": hello_data}),
                timeout=self._send_timeout_sec,
            )
        except Exception:
            logger.warning(
                "ws_broadcaster: client %d failed to receive hello frame",
                client.client_id,
                exc_info=True,
            )
        return client

    async def unregister(self, client: _ClientState) -> None:
        """Remove the client and cancel its writer task. Safe to call more
        than once (e.g. both the `WebSocketDisconnect` handler and a
        `finally` block) — a missing client id is a no-op."""
        self._clients.pop(client.client_id, None)
        if client.writer_task is not None:
            client.writer_task.cancel()
            try:
                await client.writer_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _writer_loop(self, client: _ClientState) -> None:
        """One task per connection, draining only its own queue — a slow
        or dead client can never delay another client's delivery."""
        try:
            while True:
                envelope = await client.queue.get()
                try:
                    await asyncio.wait_for(
                        client.websocket.send_json(envelope),
                        timeout=self._send_timeout_sec,
                    )
                    client.sent += 1
                    self._total_sent += 1
                except Exception:
                    # Send failed (disconnect, timeout, ...) -- this
                    # client is done; let the route's receive loop notice
                    # the disconnect and call unregister(). Never let one
                    # client's failure propagate anywhere else.
                    logger.info(
                        "ws_broadcaster: client %d send failed, ending writer loop",
                        client.client_id,
                        exc_info=True,
                    )
                    return
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def close_all(self) -> None:
        """Close every live connection and cancel every writer task.
        Called once from `backend.main`'s lifespan shutdown so a
        reload/redeploy never leaves an orphaned writer task or socket
        behind (docs/PHASE5_TICKET9_PLAN.md section 4)."""
        for client in list(self._clients.values()):
            try:
                await client.websocket.close()
            except Exception:
                pass
            await self.unregister(client)

    def stats(self) -> BroadcasterStats:
        return BroadcasterStats(
            connected_clients=len(self._clients),
            total_sent=self._total_sent,
            total_dropped=self._total_dropped,
            publish_no_loop_count=self._publish_no_loop_count,
        )
