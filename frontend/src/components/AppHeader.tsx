"use client";

import { ConnectionState } from "./ConnectionState";
import { StreamState } from "./StreamState";
import { StatChip } from "./StatChip";
import { useEventStream } from "@/lib/useEventStream";

/**
 * AppHeader (DESIGN_CONSOLE.md §5, §6) — brand, live pulse dot, stat
 * chips, speed control, inject button. The connection indicator
 * (GET /api/health) and, as of Ticket #4, the events/s and alerts stat
 * chips plus the stream indicator are wired to live data via
 * `useEventStream` (WS /ws/stream, mocked by `npm run mock` until
 * Ticket #9/#12). The Risk stat chip stays a placeholder — no single
 * "risk" figure exists yet. Speed control and inject stay disabled per
 * PHASE5_TICKET3_PLAN §1 (out of scope: Ticket #13).
 */
export function AppHeader() {
  const { status: streamStatus, eventsPerSecond, alertCount } = useEventStream();

  return (
    <header className="glass-panel flex h-14 shrink-0 items-center gap-6 rounded-none border-x-0 border-t-0 px-4">
      <div className="flex items-center gap-2">
        <span
          className="h-2 w-2 rounded-full bg-accent animate-aegis-pulse"
          aria-hidden="true"
        />
        <span className="text-sm font-bold tracking-[-0.02em] text-text">
          AEGIS
        </span>
        <ConnectionState />
        <StreamState status={streamStatus} />
      </div>

      <div className="flex items-center gap-6">
        <StatChip label="Events/s" value={String(eventsPerSecond)} tone="accent" />
        <StatChip
          label="Alerts"
          value={String(alertCount)}
          tone={alertCount > 0 ? "critical" : "text"}
        />
        <StatChip label="Risk" value="—" tone="text" />
      </div>

      <div className="ml-auto flex items-center gap-3">
        <label className="flex items-center gap-2 text-[11px] uppercase tracking-[0.08em] text-text-dim">
          Speed
          <select
            disabled
            defaultValue="1"
            aria-label="Replay speed (not yet wired — Ticket #4/#12)"
            title="Replay speed control lands with the WebSocket stream (Ticket #4/#12)"
            className="rounded-[var(--radius-dense)] border border-glass-border bg-transparent px-2 py-1 font-mono text-xs text-text-mute disabled:cursor-not-allowed"
          >
            <option value="1">1x</option>
            <option value="5">5x</option>
            <option value="20">20x</option>
          </select>
        </label>

        <button
          type="button"
          disabled
          aria-label="Inject attack scenario (not yet wired — Ticket #13)"
          title="Injection control lands in Ticket #13"
          className="rounded-[var(--radius-panel)] border border-glass-border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-mute disabled:cursor-not-allowed disabled:opacity-50"
        >
          Inject
        </button>
      </div>
    </header>
  );
}
