"use client";

import { useMemo, useEffect, useRef, useState } from "react";
import { Panel } from "../Panel";
import { useStream } from "@/lib/stream-context";
import { useTopology } from "@/lib/topology-context";
import { useGraphFocus } from "@/lib/graph-focus-context";
import { SECTOR_ORDER, buildSectorByName, groupNodesBySector, sectorLabel, CORE_SECTOR } from "@/lib/sectors";
import { SectorActivityTracker, type Severity } from "@/lib/sector-activity";
import type { TopologyResponse } from "@/lib/types";

const RENDER_INTERVAL_MS = 250;

function SectorIcon({ sector }: { sector: string }) {
  // Simple clean SVG stroke icons for each sector
  switch (sector) {
    case "energy": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>;
    case "water": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg>;
    case "transport": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>;
    case "public_safety": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>;
    case "health": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>;
    case "telecom": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M4 11a9 9 0 0 1 9 9"/><path d="M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1"/></svg>;
    case "finance": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M3 3h18v18H3z"/><path d="M3 9h18"/><path d="M9 21V9"/></svg>;
    case "civic": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>;
    case "environment": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M11 20A7 7 0 0 1 4 13v-5h5a7 7 0 0 1 7 7v5h-5z"/></svg>;
    case "monitoring": return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>;
    case CORE_SECTOR: return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>;
    default: return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/></svg>;
  }
}

interface SectorSnapshot {
  count: number;
  severity: Severity;
}

export function NTVSectorGrid() {
  const { state } = useTopology();
  const { events } = useStream();
  const { toggleFocusedSector, setExpanded } = useGraphFocus();

  const nodes: TopologyResponse["nodes"] = useMemo(
    () => (state.kind === "loaded" ? state.data.nodes : []),
    [state]
  );
  const sectorMembers = useMemo(() => groupNodesBySector(nodes), [nodes]);
  const sectorByName = useMemo(() => buildSectorByName(nodes), [nodes]);

  const trackerRef = useRef(new SectorActivityTracker());
  const eventsRef = useRef(events);
  useEffect(() => {
    eventsRef.current = events;
  }, [events]);

  const [snapshot, setSnapshot] = useState<Map<string, SectorSnapshot>>(new Map());
  useEffect(() => {
    const id = setInterval(() => {
      const tracker = trackerRef.current;
      tracker.ingest(eventsRef.current, sectorByName);
      const next = new Map<string, SectorSnapshot>();
      for (const sector of SECTOR_ORDER) {
        next.set(sector, { count: tracker.eventCountOf(sector), severity: tracker.worstSeverityOf(sector) });
      }
      setSnapshot(next);
    }, RENDER_INTERVAL_MS);
    return () => clearInterval(id);
  }, [sectorByName]);

  const activeSectors = SECTOR_ORDER.filter((s) => (sectorMembers.get(s)?.length ?? 0) > 0);

  // View-mode switching is reserved STRICTLY for the header's Non-Technical
  // / Technical toggle (AppHeader). These are in-place actions on the
  // GraphPanel that's already visible in this same view — never a tab
  // switch, so a sector card is safe to click without leaving the
  // dashboard an operator chose to be in.
  const handleSectorClick = (sector: string) => {
    setExpanded(false);
    toggleFocusedSector(sector);
  };

  const handleOpenAll = () => {
    setExpanded(true);
  };

  if (state.kind !== "loaded") {
    return <Panel label="" className="h-full flex items-center justify-center text-text-dim">Loading sectors...</Panel>;
  }

  return (
    // `content-start` + `auto-rows-min` (not `h-full`'s implicit stretch)
    // is the fix for Bug 2: with only ~2 rows of cards, a grid told to
    // fill its parent's full height distributes that height evenly
    // across those 2 rows, so each card grows to whatever vertical space
    // the layout happens to give this panel — the "excessively tall
    // cards with empty whitespace" defect. Sizing rows to their own
    // content instead lets the page give that freed space to the graph.
    <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 auto-rows-min content-start gap-2 w-full">
      {activeSectors.map((sector) => {
        const severity = snapshot.get(sector)?.severity ?? "normal";
        const isAnomalous = severity !== "normal";

        return (
          <button
            key={sector}
            onClick={() => handleSectorClick(sector)}
            className={`glass-panel px-3 py-2.5 text-left flex flex-col gap-1.5 min-h-[68px] transition-all duration-150 hover:-translate-y-0.5 hover:shadow-card-lifted focus-visible:outline-2 focus-visible:outline-accent`}
          >
            <div className="flex justify-between items-center">
              <div className="text-text-mute">
                <SectorIcon sector={sector} />
              </div>
              <div className={`h-2 w-2 rounded-full ${isAnomalous ? (severity === "critical" ? "bg-sev-critical" : "bg-sev-warning") : "bg-sev-normal"}`} />
            </div>
            <div>
              <div className="text-[11px] font-bold uppercase tracking-wider text-text truncate">
                {sectorLabel(sector)}
              </div>
              <div className="flex items-center gap-1.5">
                <span className={`h-1.5 w-1.5 rounded-full ${isAnomalous ? (severity === "critical" ? "bg-sev-critical" : "bg-sev-warning") : "bg-sev-normal"}`} />
                <span className={`text-xs font-medium ${isAnomalous ? (severity === "critical" ? "text-sev-critical" : "text-sev-warning") : "text-sev-normal"}`}>
                  {isAnomalous ? "Alert" : "Normal"}
                </span>
              </div>
            </div>
          </button>
        );
      })}

      {/* OPEN ALL Card */}
      <button
        onClick={handleOpenAll}
        className="glass-panel px-3 py-2.5 text-left flex flex-col gap-1.5 min-h-[68px] transition-all duration-150 hover:-translate-y-0.5 hover:shadow-card-lifted focus-visible:outline-2 focus-visible:outline-accent bg-ground-raised/50"
      >
        <div className="flex justify-between items-center">
          <div className="text-text-mute">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>
          </div>
          <div className="text-xs font-semibold text-accent">ALL 11</div>
        </div>
        <div>
          <div className="text-[11px] font-bold uppercase tracking-wider text-text truncate">
            OPEN ALL
          </div>
          <div className="text-xs text-text-mute group-hover:text-text-dim transition-colors">
            Expand Details →
          </div>
        </div>
      </button>
    </div>
  );
}
