"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { SeverityGlyph } from "./SeverityGlyph";
import { useGraphFocus } from "@/lib/graph-focus-context";
import { SectorActivityTracker, type Severity } from "@/lib/sector-activity";
import { SECTOR_ORDER, buildSectorByName, groupNodesBySector, sectorLabel } from "@/lib/sectors";
import { useStream } from "@/lib/stream-context";
import { useTopology } from "@/lib/topology-context";
import type { TopologyResponse } from "@/lib/types";

// SectorHealthStrip — console redesign, D-R1 (the new full-width row below
// the three main panels) + D-R3 ("one chip per sector: name, asset count,
// worst severity dot, live event count for that sector this session").
// Everything here is real:
//
//   - asset count       `GET /api/topology`'s real `sector` field, grouped
//                        via the same `groupNodesBySector` the graph uses
//                        (shared in `@/lib/sectors` so the two can never
//                        disagree about membership).
//   - worst severity /
//     event count       `SectorActivityTracker`, fed the same live event
//                        stream every other panel reads via `useStream()`
//                        — cumulative for the session, not a decaying
//                        pulse (see that file's docstring for why this is
//                        a deliberately different signal from the graph's
//                        own per-sector pulse badge).
//
// Clicking a chip focuses that sector inline in the (non-maximised) graph
// — toggles it into/out of `focusedSectors` and drops `expanded` back to
// false so the chip click has a visible effect even if the graph was
// maximised. Stackable: clicking a second chip adds it alongside whatever
// is already focused rather than replacing it (`toggleFocusedSector`).
const RENDER_INTERVAL_MS = 250;

interface SectorSnapshot {
  count: number;
  severity: Severity;
}

export function SectorHealthStrip() {
  const { state } = useTopology();
  const { events } = useStream();
  const { focusedSectors, toggleFocusedSector, setExpanded } = useGraphFocus();

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

  // Throttled ingestion, same ~100-250ms-class cadence pattern as
  // TelemetryRail/CityGraph (D10-3): read the latest event buffer via a
  // ref on a fixed interval rather than reacting to every WS message.
  // `SectorActivityTracker` is a plain mutable class (like
  // `AssetActivityTracker`/`ClusterAggregator`) read here and copied into
  // `snapshot` state — never read directly from the ref during render
  // (React disallows that; refs are for effects/handlers only).
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

  if (state.kind === "loading") {
    return (
      <div className="glass-panel flex h-16 shrink-0 items-center px-4 text-xs text-text-dim">
        Loading sector health…
      </div>
    );
  }
  if (state.kind === "error" || activeSectors.length === 0) {
    return (
      <div className="glass-panel flex h-16 shrink-0 items-center px-4 text-xs text-text-mute">
        Sector health unavailable — topology not loaded.
      </div>
    );
  }

  return (
    <div
      className="glass-panel flex h-16 shrink-0 items-stretch gap-2 overflow-x-auto px-3 py-2"
      role="group"
      aria-label="Sector health"
    >
      {activeSectors.map((sector) => {
        const members = sectorMembers.get(sector) ?? [];
        const count = snapshot.get(sector)?.count ?? 0;
        const severity = snapshot.get(sector)?.severity ?? "normal";
        const isFocused = focusedSectors.has(sector);
        return (
          <button
            key={sector}
            type="button"
            onClick={() => {
              setExpanded(false);
              toggleFocusedSector(sector);
            }}
            aria-pressed={isFocused}
            title={`${sectorLabel(sector)} — ${members.length} asset${members.length === 1 ? "" : "s"} · ${count} event${count === 1 ? "" : "s"} this session`}
            className={`flex min-w-[128px] flex-col justify-center gap-0.5 rounded-[var(--radius-dense)] border px-3 py-1 text-left transition-colors duration-150 ease-out focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${
              isFocused
                ? "border-glass-border-strong bg-glass-raised"
                : "border-glass-border hover:bg-glass-raised"
            }`}
          >
            <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-text">
              <SeverityGlyph severity={severity} />
              {sectorLabel(sector)}
            </span>
            <span className="font-mono text-[11px] tabular-nums text-text-mute">
              {members.length} asset{members.length === 1 ? "" : "s"} &middot; {count} evt
            </span>
          </button>
        );
      })}
    </div>
  );
}
