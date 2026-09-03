"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, ApiNetworkError, getEvents } from "./api";
import type {
  AlertEnvelopeData,
  CiiEnvelopeData,
  EventEnvelopeData,
  EventOut,
  HelloEnvelopeData,
  StreamEnvelope,
} from "./types";

/**
 * useEventStream — the WS /ws/stream client hook (Ticket #4).
 *
 * D4-3: stream connectivity is a SEPARATE concern from REST API
 * reachability (`@/lib/connection-context`). This hook owns its own
 * status and never reads or writes the `ConnectionProvider` state — the
 * two transports really are independent (the mock/real WS can be down
 * while /api/health is fine, and vice versa), and collapsing them into
 * one boolean is exactly the bug Ticket #3's review caught.
 *
 * Ticket #9 (absorbing Ticket #12): the default target is now the REAL
 * backend's `WS /ws/stream` (real replayed CIC-IDS2017 traffic), not the
 * Ticket #4 mock -- no synthetic data may appear anywhere in the product.
 * The mock stays in the repo as an opt-in reconnect-test fixture, reached
 * only via `NEXT_PUBLIC_WS_URL=ws://127.0.0.1:8001/ws/stream`.
 *
 * Reconnects with exponential backoff (1s -> 2s -> 4s -> 8s, capped at
 * 15s) and never spins a tight loop. `status` only ever reports
 * "reconnecting" while a reconnect timer is actually scheduled.
 *
 * Bounded buffers: this hook is a data source for Tickets #10/#11/#15,
 * none of which are wired yet, but the buffers are capped from day one
 * so a long-running dev session never leaks memory.
 */

export type StreamStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";

const MAX_BACKOFF_MS = 15_000;
const INITIAL_BACKOFF_MS = 1_000;
const MAX_EVENTS_BUFFER = 200;
const MAX_ALERTS_BUFFER = 50;
const RATE_WINDOW_MS = 1_000;

const DEFAULT_WS_URL = "ws://127.0.0.1:8000/ws/stream";

function resolveWsUrl(): string {
  return process.env.NEXT_PUBLIC_WS_URL ?? DEFAULT_WS_URL;
}

/**
 * Maps a REST `GET /api/events` row onto the same shape a live WS "event"
 * envelope carries, for reconnect backfill (see `backfillMissedEvents`
 * below). `batch_origin` is read from `raw.batch_origin` (`backend/
 * ingest.py` writes it there — see `Event.raw`'s docstring in
 * `backend/models.py`) rather than defaulted, since silently defaulting
 * an injected what-if to `"replay"` is exactly the "injected traffic
 * confused with observed telemetry" failure `batch_origin` exists to
 * prevent. `batch_index` has no REST equivalent and nothing in this
 * console renders it (confirmed: `TelemetryRail`/`CityGraph` never read
 * it), so `-1` is a safe, inert sentinel here. `is_anomaly` defaulting to
 * `false` when `EventOut.is_anomaly` is `null` is a real, if extremely
 * unlikely, edge case — `IngestPipeline` writes exactly one volumetric
 * score row per persisted event, so `null` here would mean that
 * invariant was violated, not that the event was benign; see
 * `EventOut`'s own docstring in `backend/schemas.py` for the full
 * reasoning shared with `tripwire_fired`'s default.
 */
function toEventEnvelopeData(e: EventOut): EventEnvelopeData {
  const batchOrigin =
    e.raw && typeof e.raw.batch_origin === "string" ? (e.raw.batch_origin as string) : "replay";
  return {
    id: e.id,
    ts: e.ts,
    observed_at: e.observed_at ?? e.ts,
    source_ip: e.source_id ?? "",
    destination_ip: e.destination_id ?? "",
    source_asset: e.source_asset ?? "",
    destination_asset: e.destination_asset ?? "",
    protocol: e.protocol ?? "",
    bytes: e.bytes ?? 0,
    packets: e.packets ?? 0,
    duration_sec: e.duration_sec ?? 0,
    raw_score: e.raw_score ?? 0,
    calibrated_score: e.calibrated_score ?? 0,
    is_anomaly: e.is_anomaly ?? false,
    tripwire_fired: e.tripwire_fired,
    confidence: e.confidence ?? 0,
    replay_session_id: e.replay_session_id,
    batch_index: -1,
    timing_provenance: e.timing_provenance,
    batch_origin: batchOrigin,
  };
}

export interface UseEventStreamResult {
  status: StreamStatus;
  /** Most recent events first, capped at MAX_EVENTS_BUFFER. */
  events: EventEnvelopeData[];
  /** Most recent alerts first, capped at MAX_ALERTS_BUFFER. */
  alerts: AlertEnvelopeData[];
  /** Most recent cii snapshot, if any. */
  latestCii: CiiEnvelopeData | null;
  /** Events received in roughly the last second. */
  eventsPerSecond: number;
  /** Cumulative alert count this session. */
  alertCount: number;
  /**
   * The one-time `{"type":"hello"}` snapshot the backend sends immediately
   * on connect (`ReplayStatusResponse`). `null` until the socket has
   * connected at least once. There is no periodic re-broadcast of this —
   * `day` / `total_for_day` / `speed` / `replay_session_id` stay frozen at
   * whatever they were at connect time until the next reconnect (see
   * `AppHeader`'s replay-progress display for how the live count below
   * fills the gap in between).
   */
  hello: HelloEnvelopeData | null;
  /**
   * Real "event" envelopes received on this connection since the last
   * `hello` — each one is exactly one flow the replay engine actually
   * emitted (backend/ingest.py broadcasts one "event" envelope per emitted
   * flow), so `hello.emitted_count + liveEmittedSinceHello` is a real,
   * non-fabricated running total even though the backend never re-sends
   * `emitted_count` itself. Resets to 0 on every fresh `hello` (i.e. every
   * reconnect), which is also when the `hello.emitted_count` baseline it's
   * added to gets refreshed.
   */
  liveEmittedSinceHello: number;
  /**
   * The most recently observed flow's own dataset timestamp (`ts`, not
   * `observed_at` — see `backend/replay_engine.py ReplayStatus.status()`,
   * which derives `current_virtual_position` from the same per-flow
   * virtual-time array), falling back to the `hello` snapshot's own
   * `current_virtual_position` until the first event of this connection
   * arrives. This is real data read straight off the wire, not an
   * extrapolation.
   */
  lastVirtualPosition: string | null;
  /**
   * Manually tears down the current socket (if any) and reconnects
   * immediately, bypassing any pending exponential-backoff delay and
   * resetting it back to the initial one — for the header's "Restart
   * stream" control. Safe to call at any time, including while already
   * `connected` (forces a fresh connection) or already `reconnecting`
   * (jumps the queue instead of waiting out the current backoff). A no-op
   * after unmount.
   */
  forceReconnect: () => void;
}

export function useEventStream(): UseEventStreamResult {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [events, setEvents] = useState<EventEnvelopeData[]>([]);
  const [alerts, setAlerts] = useState<AlertEnvelopeData[]>([]);
  const [latestCii, setLatestCii] = useState<CiiEnvelopeData | null>(null);
  const [eventsPerSecond, setEventsPerSecond] = useState(0);
  const [alertCount, setAlertCount] = useState(0);
  const [hello, setHello] = useState<HelloEnvelopeData | null>(null);
  const [liveEmittedSinceHello, setLiveEmittedSinceHello] = useState(0);
  const [lastVirtualPosition, setLastVirtualPosition] = useState<string | null>(null);

  // Counts events since the last rate tick; read/reset from the interval
  // below. A ref (not state) because it's a write-often, read-rarely
  // counter — routing it through setState would re-render on every
  // envelope just to track a number nothing renders directly.
  const eventsSinceTickRef = useRef(0);

  // The highest event `id` this connection has actually seen — a ref,
  // not state, because every live "event" envelope updates it and it
  // must never trigger a re-render of its own. `null` until the first
  // real event arrives, which is also what makes backfill a no-op on the
  // very first connect (there is nothing to backfill yet) without any
  // separate "is this a reconnect" bookkeeping. Persists across
  // reconnects on purpose — it is exactly what a reconnect's backfill
  // request needs to ask "what did I miss since I was last connected".
  const lastEventIdRef = useRef<number | null>(null);

  // Populated inside the effect below with a closure that tears down the
  // current socket (if any) and connects again immediately, bypassing
  // whatever exponential-backoff delay is currently pending — the "Restart
  // stream" header button (AppHeader) calls this via `forceReconnect`. A
  // ref, not state: it needs to reach into the effect's own local
  // `socket`/`backoffMs`/`reconnectTimer` closure variables, which have no
  // other way to be reached from outside the effect.
  const manualReconnectRef = useRef<() => void>(() => {});

  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let backoffMs = INITIAL_BACKOFF_MS;

    function scheduleReconnect() {
      if (cancelled) return;
      setStatus("reconnecting");
      reconnectTimer = setTimeout(() => {
        if (!cancelled) connect();
      }, backoffMs);
      backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
    }

    // Backfill (Phase A improvement pass, "Backfill missed WebSocket
    // events on reconnect") — every "event" envelope this hook has ever
    // seen updates `lastEventIdRef`, so on (re)connect this asks
    // `GET /api/events?since=<that id>` for exactly what was missed while
    // disconnected, gapless by construction (that route's `since` branch
    // is `ORDER BY id ASC` alone — see its docstring in `backend/
    // routes.py`). A no-op on the very first connect, since
    // `lastEventIdRef.current` is still `null` then. Enriched
    // (`EventOut.raw_score`/`is_anomaly`/`tripwire_fired`/etc., added
    // alongside this feature) so a backfilled row renders with the same
    // fidelity as a live envelope — never a degraded stand-in.
    async function backfillMissedEvents() {
      const since = lastEventIdRef.current;
      if (since === null) return;
      let response;
      try {
        response = await getEvents({ since, limit: MAX_EVENTS_BUFFER });
      } catch (err) {
        // A failed backfill leaves a real gap in the feed rather than a
        // fabricated one — same posture as `ws.onerror` below: nothing
        // useful to do beyond letting the next reconnect (or the next
        // live event) try again.
        console.warn(
          "AEGIS: event backfill after reconnect failed",
          err instanceof ApiNetworkError || err instanceof ApiError ? err.message : err
        );
        return;
      }
      if (cancelled || response.events.length === 0) return;

      const backfilled = response.events.map(toEventEnvelopeData);
      setEvents((prev) => {
        const seen = new Set(prev.map((e) => e.id));
        // `response.events` arrives oldest-first; folding each missed
        // event onto the front of the buffer in that order naturally
        // produces newest-first overall (the same "cons onto the front"
        // pattern the live "event" case above uses one envelope at a
        // time) — no separate reverse/sort step needed.
        let next = prev;
        for (const e of backfilled) {
          if (seen.has(e.id)) continue; // a live envelope for it may have
          // arrived while this request was in flight — never duplicate.
          seen.add(e.id);
          next = [e, ...next];
        }
        return next.length > MAX_EVENTS_BUFFER ? next.slice(0, MAX_EVENTS_BUFFER) : next;
      });

      const maxBackfilledId = backfilled.reduce((max, e) => Math.max(max, e.id), since);
      if (lastEventIdRef.current === null || maxBackfilledId > lastEventIdRef.current) {
        lastEventIdRef.current = maxBackfilledId;
      }
    }

    function connect() {
      if (cancelled) return;
      setStatus((prev) => (prev === "connected" ? prev : "connecting"));

      let ws: WebSocket;
      try {
        ws = new WebSocket(resolveWsUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      socket = ws;

      ws.onopen = () => {
        if (cancelled) return;
        backoffMs = INITIAL_BACKOFF_MS;
        setStatus("connected");
        backfillMissedEvents();
      };

      ws.onmessage = (ev) => {
        if (cancelled) return;
        let envelope: StreamEnvelope;
        try {
          envelope = JSON.parse(ev.data as string);
        } catch {
          return; // malformed frame — ignore, do not crash the stream
        }

        switch (envelope.type) {
          case "event":
            eventsSinceTickRef.current += 1;
            if (lastEventIdRef.current === null || envelope.data.id > lastEventIdRef.current) {
              lastEventIdRef.current = envelope.data.id;
            }
            setEvents((prev) => {
              const next = [envelope.data, ...prev];
              return next.length > MAX_EVENTS_BUFFER
                ? next.slice(0, MAX_EVENTS_BUFFER)
                : next;
            });
            setLiveEmittedSinceHello((n) => n + 1);
            setLastVirtualPosition(envelope.data.ts);
            break;
          case "alert":
            setAlerts((prev) => {
              const next = [envelope.data, ...prev];
              return next.length > MAX_ALERTS_BUFFER
                ? next.slice(0, MAX_ALERTS_BUFFER)
                : next;
            });
            setAlertCount((n) => n + 1);
            break;
          case "cii":
            setLatestCii(envelope.data);
            break;
          case "hello":
            setHello(envelope.data);
            setLiveEmittedSinceHello(0);
            setLastVirtualPosition(envelope.data.current_virtual_position);
            break;
        }
      };

      ws.onerror = () => {
        // The subsequent onclose handles reconnect scheduling; a
        // WebSocket error event carries no useful detail and always
        // precedes a close.
      };

      ws.onclose = () => {
        if (cancelled) return;
        socket = null;
        scheduleReconnect();
      };
    }

    // "Restart stream" (manual): clear any pending scheduled reconnect,
    // reset backoff back to the initial delay (a manual restart should
    // feel instant, never wait out a backoff grown from earlier drops),
    // and force the socket closed so the usual onclose -> reconnect path
    // doesn't fire a SECOND time on top of the immediate `connect()` this
    // triggers directly. `lastEventIdRef` is deliberately left untouched —
    // a manual restart is exactly the reconnect case `backfillMissedEvents`
    // exists for, so anything emitted while the old socket was being torn
    // down still gets picked up on the new connection's `hello`.
    manualReconnectRef.current = () => {
      if (cancelled) return;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      backoffMs = INITIAL_BACKOFF_MS;
      if (socket) {
        const old = socket;
        socket = null;
        old.onopen = null;
        old.onmessage = null;
        old.onerror = null;
        old.onclose = null;
        old.close();
      }
      connect();
    };

    // Defer the initial connect by a tick rather than calling connect()
    // synchronously. React StrictMode (dev only) double-invokes this
    // effect (mount -> cleanup -> mount) to surface non-idempotent
    // effects; a synchronous `connect()` would open a real WebSocket on
    // the throwaway first pass, and cleanup closing it mid-handshake
    // logs a browser-level "WebSocket connection failed" console error
    // that has nothing to do with actual stream health. Deferring means
    // the throwaway pass's timer is cancelled before it ever opens a
    // socket, and the second (real) pass connects normally.
    const initialConnectTimer = setTimeout(connect, 0);

    const rateTimer = setInterval(() => {
      const count = eventsSinceTickRef.current;
      eventsSinceTickRef.current = 0;
      setEventsPerSecond(count);
    }, RATE_WINDOW_MS);

    return () => {
      cancelled = true;
      manualReconnectRef.current = () => {};
      clearTimeout(initialConnectTimer);
      clearInterval(rateTimer);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
      setStatus("disconnected");
    };
  }, []);

  const forceReconnect = useCallback(() => {
    manualReconnectRef.current();
  }, []);

  return {
    status,
    events,
    alerts,
    latestCii,
    eventsPerSecond,
    alertCount,
    hello,
    liveEmittedSinceHello,
    lastVirtualPosition,
    forceReconnect,
  };
}
