"use client";

import { useState } from "react";
import { ConnectionState } from "./ConnectionState";
import { StreamState } from "./StreamState";
import { InjectControl } from "./InjectControl";
import {
  ApiError,
  ApiNetworkError,
  setReplaySpeed,
  startReplay,
  stopReplay,
} from "@/lib/api";
import { useStream } from "@/lib/stream-context";
import { useViewMode } from "@/lib/view-mode-context";
import type { HelloEnvelopeData } from "@/lib/types";

const SPEED_OPTIONS = [1, 5, 20, 60] as const;

// Console redesign (light-theme dashboard pass): the header's own
// Events/s, Alerts, Risk, and Suppressed stat chips moved to
// `MetricsStrip` (its own card row, with the same tooltips) so this bar
// stays a slim control strip — live status, replay progress, speed,
// restart, inject.

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
 * Speed control and Inject (Phase A improvement pass, roadmap "Wire the
 * Speed and Inject controls") both call the real backend now — the speed
 * `<select>` below calls `POST /api/replay/speed` directly; `Inject` opens
 * `InjectControl`, a popover backed by `GET /api/inject/scenarios` and
 * `POST /api/inject`. Both were previously permanently `disabled` per
 * PHASE5_TICKET3_PLAN §1 (Ticket #13 was out of scope at the time).
 */
export function AppHeader() {
  const {
    status: streamStatus,
    hello,
    liveEmittedSinceHello,
    lastVirtualPosition,
    forceReconnect,
  } = useStream();

  // Same "is a session live" derivation `ReplayProgress` already uses
  // below — `hello.running` is a one-time snapshot from connect/reconnect,
  // so a session started after that snapshot is detected via real traffic
  // instead (`liveEmittedSinceHello > 0`), never fabricated or polled.
  const running = (hello?.running ?? false) || liveEmittedSinceHello > 0;
  const { viewMode, setViewMode } = useViewMode();

  return (
    <header className="glass-panel flex h-20 shrink-0 items-center gap-6 rounded-none border-x-0 border-t-0 px-5">
      <div className="flex items-center gap-3 h-full">
        <div className="flex items-center justify-center border-r border-glass-border-strong h-full px-4 overflow-hidden">
          <img src="/logo1.png" alt="AEGIS" className="h-[150px] w-[150px] shrink-0 object-contain drop-shadow-sm" />
        </div>
        <span className="text-sm font-medium text-text-dim pl-1">
          Smart City Cyber Risk Monitor
        </span>
        {viewMode === "technical" && (
          <>
            <ConnectionState />
            <StreamState status={streamStatus} />
            <RestartStreamButton forceReconnect={forceReconnect} />
          </>
        )}
      </div>

      {viewMode === "non-technical" ? (
        <div className="hidden lg:block text-[13px] italic text-text-mute ml-2">
          Safer Cities. Stronger Tomorrows.
        </div>
      ) : (
        <ReplayProgress hello={hello} liveEmittedSinceHello={liveEmittedSinceHello} lastVirtualPosition={lastVirtualPosition} />
      )}

      <div className="ml-auto flex items-center gap-3">
        {/* Toggle Pill */}
        <div className="flex items-center rounded-full bg-glass-raised p-1 border border-glass-border">
          <button
            className={`ntv-toggle-button ${viewMode === "non-technical" ? "ntv-toggle-button-active" : "ntv-toggle-button-inactive"}`}
            onClick={() => setViewMode("non-technical")}
          >
            Non-Technical View
          </button>
          <button
            className={`ntv-toggle-button ${viewMode === "technical" ? "ntv-toggle-button-active" : "ntv-toggle-button-inactive"}`}
            onClick={() => setViewMode("technical")}
          >
            Technical View
          </button>
        </div>

        {viewMode === "non-technical" ? (
          <div className="flex items-center gap-2 pl-2 border-l border-glass-border">
            <span className="h-2.5 w-2.5 rounded-full bg-sev-normal" aria-hidden="true" />
            <span className="text-text-mute text-xs">●</span>
          </div>
        ) : (
          <>
            <SpeedControl currentSpeed={hello?.speed ?? null} running={running} />
            <RestartReplayButton
              day={hello?.day ?? null}
              speed={hello?.speed ?? null}
              forceReconnect={forceReconnect}
            />
            <InjectControl running={running} />
          </>
        )}
      </div>
    </header>
  );
}

/**
 * RestartStreamButton — forces a fresh WS /ws/stream connection on demand.
 * The normal reconnect path (exponential backoff, `useEventStream.ts`)
 * already recovers from a real drop on its own; this exists for the case
 * where the socket LOOKS connected but the feed has gone quiet for reasons
 * a client can't diagnose (a wedged proxy, a backend that stopped
 * broadcasting without closing the socket, etc.) — an operator's own "did
 * you try turning it off and on again" escape hatch, not a substitute for
 * the automatic recovery. `forceReconnect()` resets backoff to its initial
 * value and reconnects immediately rather than waiting out whatever delay
 * a real drop might currently have queued, and any events missed during
 * the gap are backfilled the same way a real reconnect already backfills
 * them (`GET /api/events?since=`, see useEventStream.ts).
 *
 * Disabled for a moment after each click — a debounce against spamming
 * reconnects, not a network call, so no error path is needed for it.
 */
function RestartStreamButton({ forceReconnect }: { forceReconnect: () => void }) {
  const [justClicked, setJustClicked] = useState(false);

  function handleClick() {
    forceReconnect();
    setJustClicked(true);
    setTimeout(() => setJustClicked(false), 1200);
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={justClicked}
      aria-label="Restart the live event stream"
      title="Force a fresh connection to the live stream -- use this if the feed looks stuck. Anything missed is backfilled automatically."
      className="flex h-5 w-5 shrink-0 items-center justify-center rounded-[var(--radius-dense)] text-text-dim transition-colors hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span
        className={`inline-block text-sm leading-none ${justClicked ? "animate-spin" : ""}`}
        aria-hidden="true"
      >
        ↻
      </span>
    </button>
  );
}

type RestartReplayState =
  | { kind: "idle" }
  | { kind: "confirming" }
  | { kind: "pending" }
  | { kind: "error"; message: string };

/**
 * RestartReplayButton — rewinds the REPLAY DATA to the top of the day.
 *
 * Deliberately a different control from `RestartStreamButton` above, and
 * the distinction is the whole point: `↻` restarts this browser tab's
 * *connection* to the stream (and backfills what it missed), while this
 * restarts the *data* the backend is playing. The failure they fix are
 * opposites — a socket that has gone quiet vs. a capture day that has run
 * to completion (`emitted_count == total_for_day`, engine stops itself and
 * every subsequent `POST /api/inject` 409s because nothing is running).
 * Only the second one is fixable from the backend, so it needs its own
 * button rather than overloading the reconnect icon.
 *
 * Sequence, and why it is three calls and not one:
 *   1. `stopReplay()`  — `start` returns 409 when a replay is already
 *                        running (never a silent no-op), so a restart
 *                        that skipped this would fail from the common
 *                        case of "still running, just want a rewind".
 *                        Safe when nothing is running.
 *   2. `startReplay()` — reuses the CURRENT `day`/`speed` off the last
 *                        `hello` rather than hardcoding a default, so a
 *                        restart preserves whatever the operator had set;
 *                        `null` for either falls back to the backend's own
 *                        `BACKEND_SETTINGS` default.
 *   3. `forceReconnect()` — `hello` is a one-time snapshot taken at
 *                        connect, carrying `replay_session_id`,
 *                        `emitted_count` and `total_for_day`. After a
 *                        restart the client's copy describes the OLD
 *                        session, so the header's progress readout would
 *                        keep rendering stale figures against a session
 *                        that no longer exists. Reconnecting is what makes
 *                        the UI agree with the backend again.
 *
 * Guarded by an arm-then-confirm click (auto-disarms after 3s) because
 * this is destructive to demo state — it wipes replay progress — and the
 * button sits one gap away from `Inject` in a header an operator clicks
 * under time pressure. Two deliberate clicks, no modal.
 */
function RestartReplayButton({
  day,
  speed,
  forceReconnect,
}: {
  day: string | null;
  speed: number | null;
  forceReconnect: () => void;
}) {
  const [state, setState] = useState<RestartReplayState>({ kind: "idle" });

  async function handleClick() {
    if (state.kind === "pending") return;

    if (state.kind !== "confirming") {
      setState({ kind: "confirming" });
      setTimeout(
        () => setState((s) => (s.kind === "confirming" ? { kind: "idle" } : s)),
        3000
      );
      return;
    }

    setState({ kind: "pending" });
    try {
      await stopReplay();
      await startReplay({ dataset: day, speed });
      // Only now is the old `hello` known-stale — reconnect to get a fresh
      // one describing the session that actually exists (see docstring).
      forceReconnect();
      setState({ kind: "idle" });
    } catch (err) {
      const message =
        err instanceof ApiNetworkError
          ? "Could not reach the backend — replay unchanged."
          : err instanceof ApiError
            ? err.status === 503
              ? "Replay engine unavailable (the scorer never loaded)."
              : err.status === 401
                ? "Unauthorized — this backend requires an API token (set NEXT_PUBLIC_API_TOKEN)."
                : err.status === 429
                  ? "Rate limited — wait a moment and try again."
                  : `Restart failed (HTTP ${err.status}): ${err.message}`
            : "Unknown error restarting the replay.";
      setState({ kind: "error", message });
      setTimeout(
        () => setState((s) => (s.kind === "error" ? { kind: "idle" } : s)),
        6000
      );
    }
  }

  const label =
    state.kind === "pending"
      ? "Restarting…"
      : state.kind === "confirming"
        ? "Confirm?"
        : state.kind === "error"
          ? "Failed"
          : "Restart";

  const tone =
    state.kind === "confirming"
      ? "border-accent text-accent"
      : state.kind === "error"
        ? "border-sev-critical text-sev-critical"
        : "border-glass-border text-text-dim hover:border-accent hover:text-accent";

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={state.kind === "pending"}
      aria-label={
        state.kind === "confirming"
          ? "Confirm restarting the replay from the beginning of the day"
          : "Restart the replay from the beginning of the day"
      }
      title={
        state.kind === "error"
          ? state.message
          : "Rewind the replay data to the start of the capture day. Use this when the day has run out (Inject starts returning 409). Click twice to confirm — this resets replay progress. Not the same as the ↻ next to STREAM, which only reconnects this tab."
      }
      className={`rounded-[var(--radius-panel)] border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${tone}`}
    >
      {label}
    </button>
  );
}

type SpeedSubmitState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error"; message: string };

/**
 * SpeedControl — calls `POST /api/replay/speed` on change. `currentSpeed`
 * seeds the select from the last-known server state (`hello.speed`, a
 * one-time snapshot from connect/reconnect — see `ReplayProgress`'s
 * docstring on why there is no periodic status re-broadcast to read
 * instead); once an operator changes it here, the local selection is the
 * source of truth until the next reconnect, since `POST /api/replay/speed`
 * returns the new authoritative value directly.
 */
function SpeedControl({ currentSpeed, running }: { currentSpeed: number | null; running: boolean }) {
  const [speed, setSpeed] = useState<number>(currentSpeed ?? 20);
  // Tracks the last `currentSpeed` value this component has already
  // reacted to, so the render-time adjustment below fires only when the
  // prop genuinely changes (e.g. a fresh `hello` on reconnect) — never on
  // every re-render, and never re-applying a stale snapshot over an
  // operator's own in-between manual choice. This is React's documented
  // "adjusting state when a prop changes" pattern (react.dev/learn/
  // you-might-not-need-an-effect) — no `useEffect` needed, and calling
  // `setState` here (during render, not inside an effect) is explicitly
  // supported: React re-renders immediately with the corrected state
  // before committing anything to the screen.
  const [lastSeenSpeed, setLastSeenSpeed] = useState<number | null>(currentSpeed);
  const [submit, setSubmit] = useState<SpeedSubmitState>({ kind: "idle" });

  if (currentSpeed !== lastSeenSpeed) {
    setLastSeenSpeed(currentSpeed);
    if (currentSpeed !== null && submit.kind === "idle") setSpeed(currentSpeed);
  }

  async function handleChange(next: number) {
    const prior = speed;
    setSpeed(next);
    setSubmit({ kind: "pending" });
    try {
      const res = await setReplaySpeed(next);
      setSpeed(res.speed ?? next);
      setSubmit({ kind: "idle" });
    } catch (err) {
      setSpeed(prior);
      const message =
        err instanceof ApiNetworkError
          ? "Could not reach the backend — speed unchanged."
          : err instanceof ApiError
            ? err.status === 409
              ? "No replay running — start replay before changing speed."
              : err.status === 401
                ? "Unauthorized — this backend requires an API token (set NEXT_PUBLIC_API_TOKEN)."
                : err.status === 429
                  ? "Rate limited — too many requests in a short window."
                  : `Speed change failed (HTTP ${err.status}): ${err.message}`
            : "Unknown error changing speed.";
      setSubmit({ kind: "error", message });
    }
  }

  return (
    <label
      className="flex items-center gap-2 text-[11px] uppercase tracking-[0.08em] text-text-dim"
      title={submit.kind === "error" ? submit.message : running ? "Change live replay speed" : "Replay is not running — changes will 409 until one starts"}
    >
      Speed
      <select
        value={speed}
        onChange={(e) => handleChange(Number(e.target.value))}
        disabled={submit.kind === "pending"}
        aria-label="Replay speed multiplier"
        className={`rounded-[var(--radius-dense)] border px-2 py-1 font-mono text-xs disabled:cursor-not-allowed disabled:opacity-60 ${
          submit.kind === "error" ? "border-sev-critical text-sev-critical" : "border-glass-border text-text"
        }`}
      >
        {SPEED_OPTIONS.map((v) => (
          <option key={v} value={v}>
            {v}x
          </option>
        ))}
      </select>
    </label>
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
