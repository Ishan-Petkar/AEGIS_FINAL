"use client";

import { createContext, useContext, type ReactNode } from "react";
import { useEventStream, type UseEventStreamResult } from "./useEventStream";

const StreamContext = createContext<UseEventStreamResult | null>(null);

/**
 * StreamProvider — owns the single `useEventStream()` instance (Ticket #10
 * fix round, HIGH-1). Before this, `AppHeader`, `StreamState`, and
 * `TelemetryRail` each called `useEventStream()` directly, so every tab
 * opened three independent `WS /ws/stream` connections with three
 * independent 200-event buffers. Under load those buffers can diverge —
 * the header's events/s and the feed's rows would then describe different
 * realities, exactly the class of defect Ticket #3's review caught
 * (header said CONNECTED while the graph panel said Disconnected; the
 * lesson there was ONE OWNER PER CONCERN, codified for REST reachability
 * in `ConnectionProvider` — see `@/lib/connection-context`). Mirroring
 * that pattern here: mount once near the root (`app/layout.tsx`) so every
 * consumer reads the same socket, the same buffer, and cannot disagree.
 *
 * The provider does not alter `useEventStream`'s transport, reconnect, or
 * buffering behavior — it only calls the hook once and republishes its
 * return value via context.
 */
export function StreamProvider({ children }: { children: ReactNode }) {
  const stream = useEventStream();
  return <StreamContext.Provider value={stream}>{children}</StreamContext.Provider>;
}

/** Read the shared stream state. Must be called under `StreamProvider`. */
export function useStream(): UseEventStreamResult {
  const ctx = useContext(StreamContext);
  if (!ctx) {
    throw new Error("useStream must be used within a StreamProvider");
  }
  return ctx;
}
