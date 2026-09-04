"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useTopology } from "@/lib/topology-context";
import { useStream } from "@/lib/stream-context";
import { SectorActivityTracker, type Severity } from "@/lib/sector-activity";
import { buildSectorByName } from "@/lib/sectors";
import {
  MONITORING_SECTORS,
  type MonitoringSectorDef,
} from "@/lib/monitoring-translator";

const RENDER_INTERVAL_MS = 250;

function SectorIcon({ id }: { id: string }) {
  switch (id) {
    case "energy":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
        </svg>
      );
    case "water":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z" />
        </svg>
      );
    case "transport":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <rect x="3" y="3" width="18" height="14" rx="2" />
          <path d="M3 10h18" />
          <path d="m8 21-2-4" />
          <path d="m16 21 2-4" />
        </svg>
      );
    case "public_safety":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        </svg>
      );
    case "health":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
        </svg>
      );
    case "telecom":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9" />
          <path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5" />
          <circle cx="12" cy="12" r="2" />
          <path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5" />
          <path d="M19.1 4.9C23 8.8 23 15.2 19.1 19.1" />
        </svg>
      );
    case "finance":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <rect x="2" y="5" width="20" height="14" rx="2" />
          <line x1="2" y1="10" x2="22" y2="10" />
        </svg>
      );
    case "civic":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <line x1="2" y1="22" x2="22" y2="22" />
          <line x1="12" y1="2" x2="2" y2="7" />
          <line x1="12" y1="2" x2="22" y2="7" />
          <line x1="6" y1="7" x2="6" y2="18" />
          <line x1="10" y1="7" x2="10" y2="18" />
          <line x1="14" y1="7" x2="14" y2="18" />
          <line x1="18" y1="7" x2="18" y2="18" />
        </svg>
      );
    case "environment":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z" />
          <path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12" />
        </svg>
      );
    case "monitoring":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="3" />
          <path d="M3 12h3" />
          <path d="M18 12h3" />
          <path d="M12 3v3" />
          <path d="M12 18v3" />
        </svg>
      );
    case "infrastructure":
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <rect x="2" y="2" width="20" height="8" rx="2" />
          <rect x="2" y="14" width="20" height="8" rx="2" />
          <line x1="6" y1="6" x2="6.01" y2="6" />
          <line x1="6" y1="18" x2="6.01" y2="18" />
        </svg>
      );
    default:
      return (
        <svg
          className="h-4 w-4 stroke-current fill-none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="8" x2="12" y2="12" />
          <line x1="12" y1="16" x2="12.01" y2="16" />
        </svg>
      );
  }
}

export function MonitoringSectorGrid() {
  const { state: topoState } = useTopology();
  const { events } = useStream();

  const sectorByName = useMemo(() => {
    if (topoState.kind === "loaded") {
      return buildSectorByName(topoState.data.nodes);
    }
    return new Map<string, string | null>();
  }, [topoState]);

  const trackerRef = useRef(new SectorActivityTracker());
  const eventsRef = useRef(events);
  useEffect(() => {
    eventsRef.current = events;
  }, [events]);

  const [severityBySector, setSeverityBySector] = useState<Map<string, Severity>>(new Map());

  useEffect(() => {
    const id = setInterval(() => {
      const tracker = trackerRef.current;
      tracker.ingest(eventsRef.current, sectorByName);
      const next = new Map<string, Severity>();
      for (const item of MONITORING_SECTORS) {
        next.set(item.backendSector, tracker.worstSeverityOf(item.backendSector));
      }
      setSeverityBySector(next);
    }, RENDER_INTERVAL_MS);
    return () => clearInterval(id);
  }, [sectorByName]);

  // Compute status per sector
  const sectorStatuses = useMemo(() => {
    return MONITORING_SECTORS.map((sector) => {
      const worst = severityBySector.get(sector.backendSector) ?? "normal";

      let label: "Normal" | "Degraded" | "Disrupted" = "Normal";
      let dotColorClass = "bg-sev-normal";
      let textColorClass = "text-sev-normal";

      if (worst === "critical") {
        label = "Disrupted";
        dotColorClass = "bg-sev-critical";
        textColorClass = "text-sev-critical";
      } else if (worst === "warning") {
        label = "Degraded";
        dotColorClass = "bg-sev-warning";
        textColorClass = "text-sev-warning";
      }

      return {
        sector,
        worst,
        label,
        dotColorClass,
        textColorClass,
      };
    });
  }, [severityBySector]);

  return (
    <section aria-label="City Services Overview">
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6 gap-2">
        {sectorStatuses.map(({ sector, label, dotColorClass, textColorClass, worst }) => (
          <div
            key={sector.id}
            className="glass-panel group relative flex flex-col justify-between rounded-[var(--radius-dense)] border border-glass-border p-2.5 transition-all duration-150 hover:-translate-y-0.5 hover:shadow-card hover:border-glass-border-strong"
          >
            {/* Top row: Icon and status indicator */}
            <div className="flex items-center justify-between">
              <div className="text-text-mute transition-colors duration-150 group-hover:text-accent">
                <SectorIcon id={sector.id} />
              </div>
              <span
                className={`h-1.5 w-1.5 rounded-full ${dotColorClass} ${
                  worst !== "normal" ? "animate-pulse" : ""
                }`}
                aria-hidden="true"
              />
            </div>

            {/* Bottom info: Sector Name + Status label */}
            <div className="mt-2">
              <div
                className="text-[11px] font-semibold uppercase tracking-wider text-text truncate"
                title={sector.name}
              >
                {sector.name}
              </div>
              <div className={`mt-0.5 text-[10px] font-medium ${textColorClass} flex items-center gap-1`}>
                <span className={`inline-block h-1 w-1 rounded-full ${dotColorClass}`} />
                <span>{label}</span>
              </div>
            </div>
          </div>
        ))}

        {/* 12th Slot: OPEN ALL Card */}
        <button
          type="button"
          className="glass-panel group relative flex flex-col justify-between rounded-[var(--radius-dense)] border border-dashed border-glass-border hover:border-accent hover:bg-glass-raised/60 p-2.5 transition-all duration-150 hover:-translate-y-0.5 hover:shadow-card text-left"
        >
          <div className="flex items-center justify-between">
            <div className="text-text-mute transition-colors duration-150 group-hover:text-accent">
              <svg
                className="h-4 w-4 stroke-current fill-none"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <polyline points="15 3 21 3 21 9" />
                <polyline points="9 21 3 21 3 15" />
                <line x1="21" y1="3" x2="14" y2="10" />
                <line x1="3" y1="21" x2="10" y2="14" />
              </svg>
            </div>
            <span className="text-[10px] font-semibold uppercase tracking-wider text-accent opacity-90 group-hover:opacity-100">
              ALL 11
            </span>
          </div>

          <div className="mt-2">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-accent truncate">
              OPEN ALL
            </div>
            <div className="mt-0.5 text-[10px] text-text-dim flex items-center gap-1">
              <span>Expand Details →</span>
            </div>
          </div>
        </button>
      </div>
    </section>
  );
}
