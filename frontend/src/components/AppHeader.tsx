"use client";

import { ConnectionState } from "./ConnectionState";
import { StreamState } from "./StreamState";
import { StatChip } from "./StatChip";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";
import type { HelloEnvelopeData } from "@/lib/types";

/**
 * `RISK` chip tooltip (D16-1) — the number in a header labelled RISK
 * reads as authoritative, so its exact definition is stated inline
 * rather than left for an operator to guess. Kept in one place so the
 * chip's `title` and any future surface using the same figure agree.
 */
const RISK_INDEX_TOOLTIP =
  "risk = clamp(0-100) of Σ over UNACKNOWLEDGED alerts of " +
  "(severity_weight × asset_criticality), normalised against a " +
  "configured presentation-scale constant (BACKEND_SETTINGS." +
  "risk_index_full_scale) -- not a calibrated probability. Falls when " +
  "alerts are acknowledged. Deliberately not built on CII, which is " +
  "currently near-binary across the graph.";

const ALERTS_SUPPRESSED_TOOLTIP =
  "Volumetric anomalies that were detected, scored, persisted, and " +
  "broadcast, but deliberately did NOT page an operator -- the " +
  "unsupervised channel measures ~0.02 precision on real replayed " +
  "traffic (docs/DETECTION_STUDY.md). Cumulative since the backend last " +
  "restarted.";

const ALERTS_CHIP_TOOLTIP =
  "Alerts received over this browser tab's live connection since it " +
  "last connected -- resets on reconnect. See the alerts panel for the " +
  "full, durable history.";

/**
 * AppHeader (DESIGN_CONSOLE.md §5, §6) — brand, live pulse dot, stat
 * chips, speed control, inject button. The connection indicator
 * (GET /api/health) and, as of Ticket #4, the events/s and alerts stat
 * chips plus the stream indicator are wired to live data via
 * `useStream` (the shared `StreamProvider` instance of `useEventStream`,
 * WS /ws/stream — see Ticket #10 fix round HIGH-1: every consumer reads
 * the same socket/buffer so the header and the feed can never disagree).
 *
 * The Risk chip and the Suppressed chip (Ticket #16) read `GET
 * /api/stats` via the shared `useConnection()` context, which polls it on
 * the SAME interval as `GET /api/health` (no second timer — see
 * `connection-context.tsx`). `stats === null` means "no basis to
 * compute" (no replay engine loaded yet, or the backend hasn't answered
 * the first poll) and renders `—`; a `risk_index` of `0` is a real
 * "nothing outstanding" state and renders `0`, never `—` (decision
 * D16-1). This route deliberately returns no `events/s` field of its own
 * (decision D16-2) — the Events/s chip below stays sourced from
 * `useStream()`, the one authoritative source for that figure.
 *
 * Speed control and inject stay disabled per PHASE5_TICKET3_PLAN §1
 * (out of scope: Ticket #13).
 */
export function AppHeader() {
  const { status: streamStatus, eventsPerSecond, alertCount, hello, liveEmittedSinceHello, lastVirtualPosition } =
    useStream();
  const { stats } = useConnection();

  const riskIndex = stats?.risk_index ?? null;
  const riskTone =
    riskIndex === null ? "text" : riskIndex === 0 ? "text" : riskIndex >= 50 ? "critical" : "warning";

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
          title={ALERTS_CHIP_TOOLTIP}
        />
        <StatChip
          label="Risk"
          value={riskIndex === null ? "—" : String(riskIndex)}
          tone={riskTone}
          title={RISK_INDEX_TOOLTIP}
        />
        <div className="hidden lg:flex">
          <StatChip
            label="Suppressed"
            value={stats ? String(stats.ingest.alerts_suppressed) : "—"}
            tone="text"
            title={ALERTS_SUPPRESSED_TOOLTIP}
          />
        </div>
      </div>

      <ReplayProgress hello={hello} liveEmittedSinceHello={liveEmittedSinceHello} lastVirtualPosition={lastVirtualPosition} />

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

/** `HH:MM:SS`, local time — matches `TelemetryRail`'s time formatting style minus the milliseconds (this is a coarser, header-level readout). */
function formatVirtualTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

/**
 * ReplayProgress (D-R3) — a real progress bar + capture position for
 * `ReplayEngine`'s replay of a capture day.
 *
 * There is no `GET /api/replay/status` REST route and no periodic status
 * re-broadcast (console redesign plan: the only backend touch this ticket
 * allows is the `sector` field on `/api/topology`, so adding one was out
 * of scope) — the ONLY server-confirmed `ReplayStatusResponse` snapshot is
 * the one-time `{"type":"hello"}` frame `useEventStream` receives on
 * connect (`hello`). Everything below `hello`'s own `day`/`total_for_day`/
 * `speed` is therefore frozen at whatever it was when this tab's
 * WebSocket last connected (or reconnected) — reload the page (or wait for
 * a reconnect) to pick up a replay session that started after that.
 *
 * What DOES stay live without a reconnect: `liveEmittedSinceHello` (one
 * real "event" envelope per real emitted flow, tallied since the last
 * `hello` — see `useEventStream`'s docstring) and `lastVirtualPosition`
 * (the most recently received flow's own dataset timestamp). Both are
 * read straight off the wire, never fabricated or extrapolated.
 */
function ReplayProgress({
  hello,
  liveEmittedSinceHello,
  lastVirtualPosition,
}: {
  hello: HelloEnvelopeData | null;
  liveEmittedSinceHello: number;
  lastVirtualPosition: string | null;
}) {
  const running = (hello?.running ?? false) || liveEmittedSinceHello > 0;
  const day = hello?.day ?? null;

  // Ticket #19 (§E states/widths audit): was `md:flex` (768px) — measured
  // the header's fixed-height single row overflowing horizontally at
  // 860px (the `Inject` button's right edge landed off-screen,
  // unreachable without a horizontal scroll the header never offers).
  // `lg:flex` matches the breakpoint the `Suppressed` stat chip already
  // uses for the identical reason (not enough row width below `lg`).
  if (!running && !day) {
    return (
      <div className="hidden min-w-[160px] flex-col justify-center gap-1 lg:flex" aria-live="off">
        <span className="text-[10px] uppercase tracking-[0.08em] text-text-dim">Replay</span>
        <span className="font-mono text-xs text-text-mute">idle — no session this connection</span>
      </div>
    );
  }

  const emitted = (hello?.emitted_count ?? 0) + liveEmittedSinceHello;
  const total = hello?.total_for_day ?? 0;
  const pct = total > 0 ? Math.min(100, (emitted / total) * 100) : null;
  const position = lastVirtualPosition ? formatVirtualTime(lastVirtualPosition) : null;

  return (
    <div className="hidden min-w-[200px] flex-col justify-center gap-1 lg:flex" aria-live="off">
      <div className="flex items-center justify-between gap-2 text-[10px] uppercase tracking-[0.08em] text-text-dim">
        <span>Replay {day ?? "—"}</span>
        <span className="font-mono normal-case tracking-normal text-text-mute">
          {pct !== null ? `${pct.toFixed(0)}%` : "—"}
        </span>
      </div>
      <div
        className="h-1 w-full overflow-hidden rounded-full bg-glass-border"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct !== null ? Math.round(pct) : undefined}
        aria-label={`Replay progress for ${day ?? "unknown day"}`}
      >
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-300 ease-out"
          style={{ width: `${pct ?? (running ? 100 : 0)}%`, opacity: pct !== null ? 1 : 0.35 }}
        />
      </div>
      <span className="font-mono text-[10px] tabular-nums text-text-mute">
        {emitted.toLocaleString("en-US")}
        {total > 0 ? `/${total.toLocaleString("en-US")}` : ""} flows
        {position ? ` · ${position}` : ""}
        {hello?.speed ? ` · ${hello.speed}x` : ""}
      </span>
    </div>
  );
}
