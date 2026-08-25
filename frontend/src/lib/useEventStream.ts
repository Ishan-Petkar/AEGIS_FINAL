"use client";

import { useEffect, useRef, useState } from "react";
import type { AlertEnvelopeData, CiiEnvelopeData, EventEnvelopeData, StreamEnvelope } from "./types";

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

const DEFAULT_WS_URL = "ws://127.0.0.1:8001/ws/stream";

function resolveWsUrl(): string {
  return process.env.NEXT_PUBLIC_WS_URL ?? DEFAULT_WS_URL;
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
}

export function useEventStream(): UseEventStreamResult {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [events, setEvents] = useState<EventEnvelopeData[]>([]);
  const [alerts, setAlerts] = useState<AlertEnvelopeData[]>([]);
  const [latestCii, setLatestCii] = useState<CiiEnvelopeData | null>(null);
  const [eventsPerSecond, setEventsPerSecond] = useState(0);
  const [alertCount, setAlertCount] = useState(0);

  // Counts events since the last rate tick; read/reset from the interval
  // below. A ref (not state) because it's a write-often, read-rarely
  // counter — routing it through setState would re-render on every
  // envelope just to track a number nothing renders directly.
  const eventsSinceTickRef = useRef(0);

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
            setEvents((prev) => {
              const next = [envelope.data, ...prev];
              return next.length > MAX_EVENTS_BUFFER
                ? next.slice(0, MAX_EVENTS_BUFFER)
                : next;
            });
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

  return { status, events, alerts, latestCii, eventsPerSecond, alertCount };
}
