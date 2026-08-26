"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { Panel } from "./Panel";
import { ApiError, ApiNetworkError, getTopology } from "@/lib/api";
import { useConnection } from "@/lib/connection-context";
import type { TopologyResponse } from "@/lib/types";

// react-force-graph-2d touches `window`/canvas at import time, so the
// force-directed graph (Ticket #11) is loaded client-only. This is the
// expected way to bring a canvas-based library into the Next.js App
// Router, not a workaround — deliberately deferred from Ticket #3 for
// exactly this reason (see PHASE5_TICKET11_PLAN.md §6).
const CityGraph = dynamic(() => import("./CityGraph").then((m) => m.CityGraph), {
  ssr: false,
  loading: () => (
    <div className="flex flex-col items-center gap-2 text-text-dim">
      <span
        className="h-6 w-6 animate-spin rounded-full border-2 border-glass-border-strong border-t-accent"
        aria-hidden="true"
      />
      <p className="text-sm">Loading graph renderer…</p>
    </div>
  ),
});

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; data: TopologyResponse };

/**
 * GraphPanel — hero region (DESIGN_CONSOLE.md §5). The force-directed
 * graph itself is Ticket #11's scope; this ticket wires the real
 * GET /api/topology call and renders a correct empty state with the live
 * node/edge counts, per PHASE5_TICKET3_PLAN §5.
 *
 * Connection state is NOT decided locally. `ConnectionProvider`
 * (`@/lib/connection-context`) is the single source of truth, polled via
 * GET /api/health, and this panel is a consumer of it — the same status
 * the header reads. When the shared status is "unreachable" this panel
 * shows the same disconnected state as the header, full stop, regardless
 * of whatever its own last topology fetch returned; when the shared
 * status transitions back to reachable (`reconnectEpoch` increments) the
 * panel automatically refetches topology. This is what makes "retrying"
 * an honest claim: the panel really does retry, driven by the health poll,
 * rather than fetching once on mount and getting stuck (see MEDIUM-1/
 * MEDIUM-2 in the Ticket #3 fix round).
 */
export function GraphPanel() {
  const { status, reconnectEpoch } = useConnection();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  // Bumped by the manual "Retry" button; combined with reconnectEpoch as
  // the effect's refetch trigger so a click doesn't need to call a
  // setState-invoking function directly from render.
  const [retryToken, setRetryToken] = useState(0);

  // Refetch on mount, again every time the shared connection state
  // transitions from unreachable back to reachable (reconnectEpoch), and
  // again on a manual retry click (retryToken).
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

  const isUnreachable = status === "unreachable";
  const isLoaded = !isUnreachable && state.kind === "loaded";

  return (
    <Panel
      label="City Infrastructure"
      // Below `xl` the page no longer clamps to a fixed viewport height
      // (see page.tsx), so this panel needs its own definite height for
      // the ResizeObserver-driven canvas in CityGraph to size against —
      // a fixed `h-[420px]` does that without needing document-level
      // clamping. At `xl` it reverts to flexing to fill the 3-column
      // grid row, matching the fixed-viewport behaviour that broke the
      // runaway canvas-growth loop.
      className="h-[420px] shrink-0 xl:h-auto xl:min-h-0 xl:flex-1 xl:shrink"
      bodyClassName={isLoaded ? "flex min-h-0" : "flex items-center justify-center"}
    >
      {isUnreachable && (
        <div className="flex max-w-sm flex-col items-center gap-2 text-center">
          <SeverityDot />
          <p className="text-sm font-medium text-sev-critical">
            Disconnected
          </p>
          <p className="font-mono text-xs text-text-mute">
            Backend unreachable — reconnecting automatically.
          </p>
        </div>
      )}

      {!isUnreachable && state.kind === "loading" && (
        <div className="flex flex-col items-center gap-2 text-text-dim">
          <span
            className="h-6 w-6 animate-spin rounded-full border-2 border-glass-border-strong border-t-accent"
            aria-hidden="true"
          />
          <p className="text-sm">Loading topology…</p>
        </div>
      )}

      {!isUnreachable && state.kind === "error" && (
        <div className="flex max-w-sm flex-col items-center gap-2 text-center">
          <SeverityDot />
          <p className="text-sm font-medium text-sev-critical">
            Topology unavailable
          </p>
          <p className="font-mono text-xs text-text-mute">{state.message}</p>
          <button
            type="button"
            onClick={() => setRetryToken((n) => n + 1)}
            className="mt-1 rounded-[var(--radius-dense)] border border-glass-border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim transition-colors duration-150 ease-out hover:bg-glass-raised hover:text-text"
          >
            Retry
          </button>
        </div>
      )}

      {isLoaded && state.kind === "loaded" && state.data.nodes.length > 0 && (
        <CityGraph topology={state.data} />
      )}

      {isLoaded && state.kind === "loaded" && state.data.nodes.length === 0 && (
        <div className="flex max-w-sm flex-col items-center gap-2 text-center">
          <p className="text-sm font-medium text-text-dim">Topology empty</p>
          <p className="font-mono text-xs text-text-mute">
            GET /api/topology returned 0 nodes — nothing to render.
          </p>
        </div>
      )}
    </Panel>
  );
}

function SeverityDot() {
  return (
    <span
      className="h-2.5 w-2.5 rounded-full bg-sev-critical"
      aria-hidden="true"
    />
  );
}
