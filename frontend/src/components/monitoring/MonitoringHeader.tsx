"use client";

import { useEffect, useState } from "react";
import { useView, type ViewMode } from "@/lib/view-context";
import { formatHeaderTime } from "@/lib/monitoring-translator";

export function ViewToggle({ className = "" }: { className?: string }) {
  const { viewMode, setViewMode } = useView();

  return (
    <div
      role="radiogroup"
      aria-label="Select Dashboard View"
      className={`inline-flex items-center rounded-full border border-glass-border bg-ground p-0.5 shadow-sm ${className}`}
    >
      <button
        type="button"
        role="radio"
        aria-checked={viewMode === "monitoring"}
        onClick={() => setViewMode("monitoring")}
        className={`rounded-full px-3 py-1 text-xs font-medium transition-all duration-150 ${
          viewMode === "monitoring"
            ? "bg-accent text-white shadow-sm"
            : "text-text-dim hover:text-text hover:bg-glass-raised"
        }`}
      >
        Non-Technical View
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={viewMode === "technical"}
        onClick={() => setViewMode("technical")}
        className={`rounded-full px-3 py-1 text-xs font-medium transition-all duration-150 ${
          viewMode === "technical"
            ? "bg-accent text-white shadow-sm"
            : "text-text-dim hover:text-text hover:bg-glass-raised"
        }`}
      >
        Technical View
      </button>
    </div>
  );
}

export function MonitoringHeader() {
  const [currentTime, setCurrentTime] = useState<Date | null>(null);

  useEffect(() => {
    setCurrentTime(new Date());
    const interval = setInterval(() => {
      setCurrentTime(new Date());
    }, 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <header className="glass-panel flex h-16 shrink-0 items-center justify-between gap-4 rounded-none border-x-0 border-t-0 px-6">
      {/* Left: Brand, Title, Subtitle, Tagline */}
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/10 border border-accent/20 text-accent">
          {/* Shield SVG */}
          <svg
            className="h-5 w-5 fill-none stroke-current"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            <path d="m9 12 2 2 4-4" />
          </svg>
        </div>

        <div className="flex flex-col">
          <div className="flex items-center gap-2">
            <span className="text-base font-bold tracking-tight text-text">
              AEGIS
            </span>
            <span className="hidden sm:inline-block h-3.5 w-px bg-glass-border" />
            <span className="text-xs font-medium text-text-dim">
              Smart City Cyber Risk Monitor
            </span>
          </div>
          <span className="hidden md:inline text-[11px] italic text-text-mute">
            Safer Cities. Stronger Tomorrows.
          </span>
        </div>
      </div>

      {/* Right: View Toggle, Status dot, Live Clock */}
      <div className="flex items-center gap-4">
        <ViewToggle />

        <div className="hidden sm:flex items-center gap-2 border-l border-glass-border pl-4">
          <span
            className="h-2 w-2 rounded-full bg-sev-normal animate-pulse"
            title="Live telemetry active"
            aria-label="Live telemetry active"
          />
          <span className="font-mono text-xs text-text-dim">
            {currentTime ? formatHeaderTime(currentTime) : "—"}
          </span>
        </div>
      </div>
    </header>
  );
}
