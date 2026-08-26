"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { ApiError, ApiNetworkError, getTopology } from "@/lib/api";
import { useConnection } from "@/lib/connection-context";
import type { TopologyResponse } from "@/lib/types";

/**
 * TopologyProvider — console redesign (docs/PHASE5_CONSOLE_REDESIGN_PLAN.md
 * §3, §4). `GET /api/topology` used to be fetched privately inside
 * `GraphPanel`; the sector health strip (D-R3) needs the exact same
 * node/edge set (sector membership, criticality, per-sector asset counts)
 * to stay in lockstep with what the graph itself is drawing, so this lifts
 * the fetch to a shared provider — the same "one owner per concern" pattern
 * `ConnectionProvider`/`StreamProvider` already use, for the same reason:
 * two components independently fetching and deciding "what is the
 * topology" could disagree (e.g. mid-request, or after a failed retry in
 * only one of them).
 *
 * Refetch triggers are identical to the panel's previous behavior: on
 * mount, whenever the shared REST connection transitions from unreachable
 * back to reachable (`reconnectEpoch`), and on manual retry.
 */
type TopologyState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; data: TopologyResponse };

interface TopologyContextValue {
  state: TopologyState;
  retry: () => void;
}

const TopologyContext = createContext<TopologyContextValue | null>(null);

export function TopologyProvider({ children }: { children: ReactNode }) {
  const { reconnectEpoch } = useConnection();
  const [state, setState] = useState<TopologyState>({ kind: "loading" });
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setState({ kind: "loading" });
      try {
        const data = await getTopology();
        if (cancelled) return;
        setState({ kind: "loaded", data });
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiNetworkError
            ? "Could not reach the backend for this request."
            : err instanceof ApiError
              ? `Topology request failed (HTTP ${err.status}): ${err.message}`
              : "Unknown error loading topology";
        setState({ kind: "error", message });
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [reconnectEpoch, retryToken]);

  const retry = useCallback(() => setRetryToken((n) => n + 1), []);

  return (
    <TopologyContext.Provider value={{ state, retry }}>{children}</TopologyContext.Provider>
  );
}

/** Read the shared topology state. Must be called under `TopologyProvider`. */
export function useTopology(): TopologyContextValue {
  const ctx = useContext(TopologyContext);
  if (!ctx) {
    throw new Error("useTopology must be used within a TopologyProvider");
  }
  return ctx;
}
