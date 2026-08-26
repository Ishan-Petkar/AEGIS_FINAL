"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Panel } from "./Panel";
import { SeverityGlyph, type Severity } from "./SeverityGlyph";
import { useStream } from "@/lib/stream-context";
import type { EventEnvelopeData } from "@/lib/types";

// Ticket #10 replaces the Ticket #3 static placeholder with the real
// stream (useEventStream, WS /ws/stream). Three binding decisions from
// docs/PHASE5_TICKET10_PLAN.md:
//
// D10-1 newest-at-top, no scroll-to-bottom. The hook already returns
// newest-first; we render it in that order and fade opacity downward
// (freshest -> --text-mute).
//
// D10-2 freeze on interaction. Hover or scroll inside the feed freezes
// the rendered rows and shows an honest "PAUSED · N new" count computed
// from real event ids the hook has received since the freeze. This is
// display-only: the hook keeps consuming and the header's events/s
// keeps ticking regardless of freeze state.
//
// D10-3 throttle rendering, not receiving. The hook's `events` array
// updates on every message; copying it into local render state on every
// change would still re-render at message rate. Instead the latest
// array is mirrored into a ref every render (cheap, no re-render) and a
// fixed ~100ms interval copies that ref into the state that actually
// drives the row list, so DOM updates are batched regardless of how
// fast the stream is producing events.

const RENDER_INTERVAL_MS = 100;

/** HH:MM:SS.mmm, local time, mono/tabular-nums in the row markup. */
function formatTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}

/**
 * Prefer the resolved asset name; fall back to the raw IP when the asset
 * is an auto-registered `Unresolved_<id>` (K8: most real replay traffic
 * resolves this way, and showing that string verbatim fills the feed
 * with noise — the IP is the more informative of the two).
 */
function displayIdentity(asset: string, ip: string): string {
  if (!asset || asset.startsWith("Unresolved_")) return ip;
  return asset;
}

function severityOf(e: EventEnvelopeData): Severity {
  if (e.tripwire_fired) return "critical";
  if (e.is_anomaly) return "warning";
  return "normal";
}

/**
 * TelemetryRail (DESIGN_CONSOLE.md §5, §6) — feed rows, 28px tall, mono,
 * `time · src → dst · glyph`. Live data from `useStream()` (the shared
 * `StreamProvider` instance — Ticket #10 fix round HIGH-1); see
 * module-level comment above for the D10-1/2/3 rationale.
 */
export function TelemetryRail() {
  const { status, events } = useStream();

  // Mirrors `events` on every change without itself causing a re-render —
  // read by the throttled interval below.
  const eventsRef = useRef(events);
  useEffect(() => {
    eventsRef.current = events;
  }, [events]);

  const [displayEvents, setDisplayEvents] = useState<EventEnvelopeData[]>(events);
  const displayEventsRef = useRef(displayEvents);
  useEffect(() => {
    displayEventsRef.current = displayEvents;
  }, [displayEvents]);

  const [frozen, setFrozen] = useState(false);
  const frozenRef = useRef(false);
  useEffect(() => {
    frozenRef.current = frozen;
  }, [frozen]);

  const [pendingCount, setPendingCount] = useState(0);
  const frozenHeadIdRef = useRef<number | null>(null);

  useEffect(() => {
    const interval = setInterval(() => {
      if (!frozenRef.current) {
        setDisplayEvents((prev) => {
          const latest = eventsRef.current;
          if (prev.length === latest.length && prev[0]?.id === latest[0]?.id) {
            return prev;
          }
          return latest;
        });
      } else {
        const headId = frozenHeadIdRef.current;
        const count =
          headId == null ? 0 : eventsRef.current.filter((e) => e.id > headId).length;
        setPendingCount((prev) => (prev === count ? prev : count));
      }
    }, RENDER_INTERVAL_MS);
    return () => clearInterval(interval);
  }, []);

  const freeze = useCallback(() => {
    if (!frozenRef.current) {
      frozenHeadIdRef.current = displayEventsRef.current[0]?.id ?? null;
      setFrozen(true);
    }
  }, []);

  const resume = useCallback(() => {
    frozenHeadIdRef.current = null;
    setPendingCount(0);
    setFrozen(false);
    setDisplayEvents(eventsRef.current);
  }, []);

  const hasRows = displayEvents.length > 0;

  let stateMessage: string | null = null;
  if (!hasRows) {
    if (status === "connecting") stateMessage = "Connecting to stream…";
    else if (status === "connected") stateMessage = "Connected — waiting for events";
    else if (status === "reconnecting") stateMessage = "Reconnecting to stream…";
    else stateMessage = "Disconnected";
  }

  const banner =
    hasRows && status !== "connected"
      ? status === "reconnecting"
        ? "Reconnecting — showing last received events"
        : "Disconnected — showing last received events"
      : null;

  return (
    <Panel
      label="Telemetry"
      // Console redesign (D-R1): narrowed from 340px so the graph — the
      // hero region — gets the width back. Rows are `time · src→dst ·
      // glyph`; they still fit at 280px.
      className="w-full lg:w-[280px] lg:shrink-0"
      action={
        frozen ? (
          <button
            type="button"
            onClick={resume}
            aria-live="polite"
            className="rounded-[var(--radius-dense)] border border-glass-border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-sev-warning"
          >
            Paused &middot; {pendingCount} new
          </button>
        ) : undefined
      }
    >
      <div
        className="h-full min-h-0 overflow-y-auto"
        onMouseEnter={freeze}
        onMouseLeave={resume}
        onScroll={freeze}
      >
        {banner && (
          <p className="px-2 pb-1 text-[10px] uppercase tracking-[0.08em] text-sev-warning">
            {banner}
          </p>
        )}
        {stateMessage ? (
          <p className="px-2 py-3 text-xs text-text-mute">{stateMessage}</p>
        ) : (
          <ul className="flex flex-col">
            {displayEvents.map((e, i) => {
              const severity = severityOf(e);
              const isAnomalous = severity !== "normal";
              const opacity = Math.max(0.35, 1 - i * 0.04);
              const src = displayIdentity(e.source_asset, e.source_ip);
              const dst = displayIdentity(e.destination_asset, e.destination_ip);
              return (
                <li
                  key={e.id}
                  className={`flex h-7 items-center gap-2 border-b border-glass-border/50 pl-2 font-mono text-xs ${
                    isAnomalous ? "border-l-2" : "border-l-2 border-l-transparent"
                  }`}
                  style={{
                    opacity,
                    borderLeftColor: isAnomalous
                      ? severity === "critical"
                        ? "var(--sev-critical)"
                        : "var(--sev-warning)"
                      : undefined,
                  }}
                >
                  <span className="shrink-0 tabular-nums text-text-mute">
                    {formatTime(e.ts)}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-text-dim">
                    {src} <span aria-hidden="true">&rarr;</span> {dst}
                  </span>
                  {/* Ticket #13 what-if scenarios replay REAL captured
                      attack flows re-targeted onto a curated asset. That
                      is an operator hypothesis, not observed telemetry,
                      and the feed must never let the two look alike. */}
                  {e.batch_origin === "injected" && (
                    <span
                      className="shrink-0 rounded-sm border border-accent/50 px-1 text-[10px] uppercase tracking-wider text-accent"
                      title="Operator what-if: real captured attack flows re-targeted onto a curated asset — not observed telemetry"
                    >
                      inject
                    </span>
                  )}
                  <SeverityGlyph severity={severity} className="ml-auto shrink-0" />
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Panel>
  );
}
