"use client";

import dynamic from "next/dynamic";
import { Panel } from "./Panel";
import { useState } from "react";
import { useConnection } from "@/lib/connection-context";
import { useGraphFocus } from "@/lib/graph-focus-context";
import { useTopology } from "@/lib/topology-context";

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

/**
 * GraphPanel — hero region (DESIGN_CONSOLE.md §5). Topology is now fetched
 * once by the shared `TopologyProvider` (console redesign — the sector
 * health strip needs the same node/edge set), so this panel is a plain
 * consumer of `useTopology()`; the loading/error/empty branches below are
 * unchanged from the original per-panel fetch, per PHASE5_TICKET3_PLAN §5.
 *
 * Connection state is NOT decided locally. `ConnectionProvider`
 * (`@/lib/connection-context`) is the single source of truth, polled via
 * GET /api/health, and this panel is a consumer of it — the same status
 * the header reads. When the shared status is "unreachable" this panel
 * shows the same disconnected state as the header, full stop, regardless
 * of whatever the last topology fetch returned.
 *
 * The maximise control (`⤢`, D-R2) toggles `GraphFocusProvider`'s shared
 * `expanded` flag: `false` renders the panel in its normal 3-column slot
 * showing the sector-aggregated default view; `true` renders it as a
 * `position: fixed` full-viewport overlay (independent of its flex
 * ancestors — `fixed` breaks out of the layout regardless of parent rules)
 * showing every curated asset, per §1's "Expanded" geometry target. Escape
 * also collapses it back (wired in `CityGraph`, which owns the keydown
 * listener since it already tracks focus/hover state).
 */
export function GraphPanel() {
  const { status } = useConnection();
  const { state, retry } = useTopology();
  const { expanded, setExpanded } = useGraphFocus();
  const [viewMode, setViewMode] = useState<"city" | "finance">("city");

  const isUnreachable = status === "unreachable";
  const isLoaded = !isUnreachable && state.kind === "loaded";

  return (
    <>
      {/* Console redesign: a full-viewport takeover needs an opaque
          backing, not `.glass-panel`'s deliberately near-transparent
          fill (`--glass`, ~4.5% alpha — right for a small panel floating
          over the page's own ambient glow, wrong for a "maximised"
          view that's meant to fully replace what's on screen). Without
          this, the other panels sitting underneath in normal document
          flow visibly show through, which reads as a rendering bug, not
          the intended "the graph now owns the whole window." A separate
          element (not just an opaque className on the Panel itself)
          because the Panel keeps its normal glass styling — this is
          purely the backdrop it sits on. `z-40`, one below the Panel's
          own `z-50`. */}
      {expanded && <div className="fixed inset-0 z-40 bg-ground" aria-hidden="true" />}
      <Panel
      label={viewMode === "city" ? "City Infrastructure" : "FINANCIAL INFRASTRUCTURE MAP"}
      action={
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 rounded-[var(--radius-dense)] bg-glass-raised p-0.5">
            <button
              type="button"
              onClick={() => setViewMode("city")}
              className={`rounded-[var(--radius-dense)] px-3 py-1 text-xs font-semibold transition-colors duration-150 ease-out ${
                viewMode === "city"
                  ? "bg-white text-black shadow-sm"
                  : "text-text-dim hover:text-text"
              }`}
            >
              City View
            </button>
            <button
              type="button"
              onClick={() => setViewMode("finance")}
              className={`rounded-[var(--radius-dense)] px-3 py-1 text-xs font-semibold transition-colors duration-150 ease-out ${
                viewMode === "finance"
                  ? "bg-sev-normal text-white shadow-sm"
                  : "text-text-dim hover:text-text"
              }`}
            >
              Finance View
            </button>
          </div>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-label={expanded ? "Collapse graph to normal size" : "Maximise graph to full window"}
            aria-pressed={expanded}
            title={expanded ? "Collapse (Esc)" : "Maximise — all 50 curated assets"}
            className="rounded-[var(--radius-dense)] border border-glass-border px-2 py-1 text-xs text-text-dim transition-colors duration-150 ease-out hover:bg-glass-raised hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            {expanded ? "⤡" : "⤢"}
          </button>
        </div>
      }
      // Below `xl` the page no longer clamps to a fixed viewport height
      // (see page.tsx), so this panel needs its own definite height for
      // the ResizeObserver-driven canvas in CityGraph to size against —
      // a fixed `h-[420px]` does that without needing document-level
      // clamping. At `xl` it reverts to flexing to fill the 3-column
      // grid row, matching the fixed-viewport behaviour that broke the
      // runaway canvas-growth loop. When `expanded`, none of that applies
      // — `fixed inset-0` takes the panel out of flow entirely.
      className={
        expanded
          ? "fixed inset-0 z-50 h-dvh w-dvw rounded-none"
          : "h-[420px] shrink-0 xl:h-auto xl:min-h-0 xl:flex-1 xl:shrink"
      }
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
            onClick={retry}
            className="mt-1 rounded-[var(--radius-dense)] border border-glass-border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim transition-colors duration-150 ease-out hover:bg-glass-raised hover:text-text"
          >
            Retry
          </button>
        </div>
      )}

      {isLoaded && state.kind === "loaded" && state.data.nodes.length > 0 && (
        <CityGraph topology={state.data} viewMode={viewMode} />
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
    </>
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
