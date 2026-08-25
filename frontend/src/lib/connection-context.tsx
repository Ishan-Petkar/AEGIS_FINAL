"use client";

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { getHealth } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";

export type ConnectionStatus = "connecting" | "connected" | "degraded" | "unreachable";

const POLL_INTERVAL_MS = 5000;

interface ConnectionContextValue {
  status: ConnectionStatus;
  health: HealthResponse | null;
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
      value={{ status, health, isReachable, reconnectEpoch }}
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
