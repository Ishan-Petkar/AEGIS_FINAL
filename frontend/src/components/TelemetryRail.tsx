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

/**
 * Ticket #19 (§A): `.mmm` is dropped unconditionally — every event on
 * this capture day scores `.000`, so rendering it claims precision the
 * data does not have. Seconds are kept only when `timing_provenance`
 * says the event's `ts` actually carries capture-time seconds
 * (`capture_seconds`); a day replayed from `interpolated_minute_bucket`
 * rows has `:00` on every single row for the same reason, so seconds are
 * dropped there too. Never hardcode which — always read the per-event
 * field, since a future dataset could mix both within one replay.
 */
function formatTime(ts: string, timingProvenance: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (timingProvenance === "interpolated_minute_bucket") return `${hh}:${mm}`;
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
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

/** The LAN subnet this capture's internal hosts sit on (config.py /
 * `AssetRegistry`'s 10.0.1.x proximity rule aside — the raw source data
 * itself uses 192.168.10.x). Only ever compared against, never guessed. */
const LAN_PREFIX = "192.168.";

/**
 * Ticket #19 (§A): when BOTH endpoints render as bare IPs (neither
 * resolved to a curated asset) and share the `192.168.` LAN prefix, drop
 * that shared prefix from both — it is redundant on every such row and
 * frees the characters the 280px rail needs. Never trimmed for a
 * resolved asset name, and never trimmed unless BOTH sides actually
 * share it — an elided address must still be the FULL remaining
 * identity, not a shortened alias. `elided` is surfaced via the row's
 * tooltip so the omission is visible, never silent.
 */
function addressPair(
  srcAsset: string,
  srcIp: string,
  dstAsset: string,
  dstIp: string
): { src: string; dst: string; elided: boolean } {
  const src = displayIdentity(srcAsset, srcIp);
  const dst = displayIdentity(dstAsset, dstIp);
  // Elision is deliberately DISABLED. Trimming the shared `192.168.`
  // prefix did make rows fit, but it rendered `192.168.10.5` as `10.5`,
  // which the surrounding CSS then truncated further to `…10.5` — an
  // address you cannot act on. The rail was widened to 320px instead (see
  // the Panel className below): the feed's entire purpose is showing who
  // talked to whom, so buying "nothing clips" with the identity itself is
  // the wrong trade. Kept as a named no-op rather than deleted, so the
  // rejected approach and its reason stay visible.
  void LAN_PREFIX;
  return { src, dst, elided: false };
}

function severityOf(e: EventEnvelopeData): Severity {
  if (e.tripwire_fired) return "critical";
  if (e.is_anomaly) return "warning";
  return "normal";
}

// Phase 6: TYPE column — precedence-ordered tag from per-event detection signals.
// Never surfaces `Event.raw.label` (attack ground truth, must not mislead operators
// about what AEGIS itself can observe in real time).
export type TrafficTag =
  | "TRIPWIRE"
  | "KNOWN-BAD"
  | "C2-SHAPED"
  | "ADMIN-PORT"
  | "DB-EXPOSED"
  | "BEACON"
  | "KNOWN-THREAT"
  | "ANOMALY"
  | "NORMAL";

// Signature rule ID → traffic tag. Stable IDs per signature.py:250–253.
const SIG_TAG: Record<string, TrafficTag> = {
  "AEGIS-SIG-001": "KNOWN-BAD",
  "AEGIS-SIG-002": "C2-SHAPED",
  "AEGIS-SIG-004": "ADMIN-PORT",
  "AEGIS-SIG-005": "DB-EXPOSED",
};

function typeTagOf(e: EventEnvelopeData): TrafficTag {
  if (e.tripwire_fired) return "TRIPWIRE";
  // Signature-based tags from hybrid.matched_rules (Phase 6.1 backend addition);
  // fall back gracefully if the backend hasn’t been updated yet.
  if (e.hybrid?.matched_rules) {
    for (const ruleId of e.hybrid.matched_rules) {
      const tag = SIG_TAG[ruleId];
      if (tag) return tag;
    }
  }
  // Beaconing detector fired.
  if (e.hybrid?.fired_detectors?.includes("beaconing")) return "BEACON";
  // Any random-forest/ML verdict.
  if (e.hybrid?.fired_detectors?.some((d) => d.includes("random_forest") || d.includes("gradient_boost")))
    return "KNOWN-THREAT";
  if (e.is_anomaly) return "ANOMALY";
  return "NORMAL";
}

const TAG_STYLE: Record<TrafficTag, string> = {
  TRIPWIRE:   "border-sev-critical text-sev-critical",
  "KNOWN-BAD":  "border-sev-critical text-sev-critical",
  "C2-SHAPED":  "border-sev-warning text-sev-warning",
  "ADMIN-PORT": "border-sev-warning text-sev-warning",
  "DB-EXPOSED": "border-sev-warning text-sev-warning",
  BEACON:      "border-sev-warning text-sev-warning",
  "KNOWN-THREAT": "border-sev-warning text-sev-warning",
  ANOMALY:     "border-sev-warning text-sev-warning",
  NORMAL:      "border-glass-border text-text-mute",
};

function TrafficTypeBadge({ tag }: { tag: TrafficTag }) {
  return (
    <span
      className={`shrink-0 rounded border px-1 text-[9px] font-bold uppercase tracking-[0.06em] ${TAG_STYLE[tag]}`}
      aria-label={`Traffic type: ${tag}`}
    >
      {tag}
    </span>
  );
}

/**
 * TelemetryRail (DESIGN_CONSOLE.md §5, §6) — feed rows, mono, two lines:
 * `time · glyph` then the full `src → dst` address pair (Ticket #19 §A —
 * a single 28px line cannot hold two un-elided capture-day IPs at 280px
 * without truncating the destination, so DESIGN_CONSOLE's row height is
 * deliberately not followed here; see the row's own comment). Live data
 * from `useStream()` (the shared `StreamProvider` instance — Ticket #10
 * fix round HIGH-1); see module-level comment above for the D10-1/2/3
 * rationale.
 */
export function TelemetryRail() {
  const { status, events } = useStream();
  const [typeFilter, setTypeFilter] = useState<TrafficTag | "ALL">("ALL");

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

  const filteredEvents =
    typeFilter === "ALL" ? displayEvents : displayEvents.filter((e) => typeTagOf(e) === typeFilter);

  const hasRows = filteredEvents.length > 0 || displayEvents.length > 0;

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
      // hero region — gets the width back. Ticket #19 (§A): a single
      // `time · src → dst · glyph` line cannot fit two full, un-elided
      // capture-day IP addresses at 280px (measured: two unrelated dotted
      // -quad addresses alone need ~200px, before the timestamp or glyph)
      // without truncating the destination — which this project treats as
      // a correctness bug, not a layout nicety. Rows are two lines instead
      // (time/glyph, then the full address pair) so the address line never
      // needs to claim less identity than it has.
      //
      // Ticket #19 (§C, deferred from #11): below `xl` the page drops its
      // fixed-viewport clamp (`page.tsx`'s `xl:h-full`) so panels stack in
      // normal document flow instead of clipping — but that leaves this
      // Panel with no definite ancestor height at all, so the inner
      // `overflow-y-auto` div (sized via `h-full`) has nothing to bound
      // against and all 200 buffered rows render inline, growing the page
      // very long (nothing clipped or unreachable, just a comfort defect).
      // `h-[420px]` below `xl` gives it the same definite height
      // `GraphPanel` already gives itself for the identical reason, so the
      // feed gets its own internal scrollbar at stacked widths too. Width
      // behaviour (`w-full` stacked, fixed 280px and non-growing from
      // `lg` up, same as before this ticket) is unchanged — only height
      // is new here.
      // 320px, not the redesign's 280px. Narrowing to 280 to give the
      // graph width made every one of the 200 feed rows clip its
      // destination, and the first fix for that elided the address to
      // `…10.5` — which satisfies "nothing is clipped" by deleting the
      // identity rather than fitting it. A feed whose whole job is
      // showing who talked to whom must not do that, so the rail takes
      // back the 40px it actually needs and addresses render in full.
      className="h-[420px] w-full shrink-0 lg:w-[320px] lg:shrink-0 xl:h-auto"
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
        <div className="flex shrink-0 items-center gap-2 border-b border-glass-border px-2 py-1.5">
          <span className="text-[9px] font-semibold uppercase tracking-[0.07em] text-text-mute">Filter</span>
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value as TrafficTag | "ALL")}
            className="ml-auto rounded border border-glass-border bg-glass-raised px-1.5 py-0.5 text-[10px] font-mono text-text-dim focus:outline-none focus:ring-1 focus:ring-accent"
            aria-label="Filter traffic by type"
          >
            <option value="ALL">All Traffic</option>
            <option value="TRIPWIRE">Tripwire</option>
            <option value="KNOWN-BAD">Known-Bad</option>
            <option value="C2-SHAPED">C2-Shaped</option>
            <option value="BEACON">Beacon</option>
            <option value="KNOWN-THREAT">Known-Threat</option>
            <option value="ANOMALY">Anomaly</option>
            <option value="NORMAL">Normal</option>
          </select>
        </div>
        {banner && (
          <p className="px-2 pb-1 text-[10px] uppercase tracking-[0.08em] text-sev-warning">
            {banner}
          </p>
        )}
        {stateMessage ? (
          <p className="px-2 py-3 text-xs text-text-mute">{stateMessage}</p>
        ) : filteredEvents.length === 0 && typeFilter !== "ALL" ? (
          <p className="px-2 py-3 text-xs text-text-mute">No {typeFilter} events in current view.</p>
        ) : (
          <ul className="flex flex-col">
            {filteredEvents.map((e, i) => {
              const severity = severityOf(e);
              const isAnomalous = severity !== "normal";
              const opacity = Math.max(0.75, 1 - i * 0.04);
              const tag = typeTagOf(e);
              const { src, dst, elided } = addressPair(
                e.source_asset,
                e.source_ip,
                e.destination_asset,
                e.destination_ip
              );
              const fullSrc = displayIdentity(e.source_asset, e.source_ip);
              const fullDst = displayIdentity(e.destination_asset, e.destination_ip);
              return (
                <li
                  key={e.id}
                  className={`flex flex-col gap-0.5 border-b border-glass-border py-1 pl-2 font-mono text-xs ${
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
                  <div className="flex items-center gap-2">
                    <span className="shrink-0 tabular-nums text-text-mute">
                      {formatTime(e.ts, e.timing_provenance)}
                    </span>
                    {/* Phase 6: TYPE tag */}
                    <TrafficTypeBadge tag={tag} />
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
                  </div>
                  <span
                    className="min-w-0 truncate text-text-dim"
                    title={
                      elided
                        ? `${fullSrc} → ${fullDst} (shared ${LAN_PREFIX} prefix omitted above)`
                        : `${fullSrc} → ${fullDst}`
                    }
                  >
                    {elided && (
                      <span className="text-text-mute" aria-hidden="true">
                        &hellip;
                      </span>
                    )}
                    {src} <span aria-hidden="true">&rarr;</span> {dst}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Panel>
  );
}
