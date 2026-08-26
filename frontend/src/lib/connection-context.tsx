"use client";

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { getHealth, getStats } from "@/lib/api";
import type { HealthResponse, StatsResponse } from "@/lib/types";

export type ConnectionStatus = "connecting" | "connected" | "degraded" | "unreachable";

const POLL_INTERVAL_MS = 5000;

interface ConnectionContextValue {
  status: ConnectionStatus;
  health: HealthResponse | null;
  /**
   * GET /api/stats (Ticket #16), polled on the SAME interval as `health`
   * above — reusing this provider's existing cadence rather than standing
   * up a second timer (docs/PHASE5_TICKET16_PLAN.md §6). `null` until the
   * first successful response, and again whenever the backend answers a
   * real 503 (no replay engine — scorer never loaded): that is "no basis
   * to compute" (render `—`), distinct from a `risk_index` of `0`, which
   * `StatsResponse` returns explicitly when there are simply no
   * unacknowledged alerts — a real, meaningful state, not an absence of
   * one. A stats fetch failure never flips `status` below — reachability
   * is governed solely by `health`, matching D4-3's "stream connectivity
   * is a separate concern" precedent applied here to the stats endpoint.
   */
  stats: StatsResponse | null;
  /** True once the health endpoint has answered at all, even if degraded. */
  isReachable: boolean;
  /**
   * Increments every time the backend transitions from unreachable back to
   * reachable. Data-bound panels should depend on this value (in addition
   * to mount) to refetch after an outage — see MEDIUM-1 in the Ticket #3
   * fix round: two components independently deciding "am I connected?"
   * will always be able to disagree, so this context is the single source
   * of truth and panels must react to its transitions rather than polling
   * on their own.
   */
  reconnectEpoch: number;
}

const ConnectionContext = createContext<ConnectionContextValue | null>(null);

/**
 * ConnectionProvider — owns the GET /api/health poll (Ticket #3 scope, not
 * a WebSocket; that's Ticket #4/#12) and is the single source of truth for
 * "is the backend reachable". Mount once near the root (see
 * `app/layout.tsx`) so every consumer — the header indicator and every
 * data-bound panel — reads the same status and cannot disagree with it.
 */
export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [reconnectEpoch, setReconnectEpoch] = useState(0);
  const wasUnreachable = useRef(false);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const result = await getHealth();
        if (cancelled) return;
        setHealth(result);
        setStatus(result.status === "ok" ? "connected" : "degraded");
        if (wasUnreachable.current) {
          wasUnreachable.current = false;
          setReconnectEpoch((n) => n + 1);
        }
      } catch {
        if (cancelled) return;
        setHealth(null);
        setStatus("unreachable");
        wasUnreachable.current = true;
      }

      // Same tick as the health check above (D16's "reuse the existing
      // cadence, don't add a second timer") but its own try/catch: a
      // backend that is reachable but has no replay engine loaded
      // answers /api/health fine and /api/stats with a real 503, and
      // that must not be misread as the backend being unreachable.
      try {
        const result = await getStats();
        if (cancelled) return;
        setStats(result);
      } catch {
        if (cancelled) return;
        setStats(null);
      }
    }

    poll();
    const id = window.setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const isReachable = status === "connected" || status === "degraded";

  return (
    <ConnectionContext.Provider
      value={{ status, health, stats, isReachable, reconnectEpoch }}
    >
      {children}
    </ConnectionContext.Provider>
  );
}

/** Read the shared connection state. Must be called under `ConnectionProvider`. */
export function useConnection(): ConnectionContextValue {
  const ctx = useContext(ConnectionContext);
  if (!ctx) {
    throw new Error("useConnection must be used within a ConnectionProvider");
  }
  return ctx;
}
