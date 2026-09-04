"use client";

import type { ReactElement } from "react";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";

/**
 * MetricsStrip — the four headline numbers, pulled out of AppHeader into
 * their own row of cards (console redesign, light-theme dashboard pass)
 * so the header stays a slim control bar and the numbers an operator
 * actually watches get real visual weight, matching the reference
 * layout's top metric-card row.
 *
 * All four are real, already-computed values this console already had
 * (nothing new fetched here): Events/s and Active Alerts come from the
 * shared `useStream()` WebSocket state (the same source AppHeader used
 * before this pass), System Risk from `GET /api/stats` via
 * `useConnection()`, and Mean Lead Time is the project's own documented,
 * measured constant (docs/DETECTION_STUDY.md — the honeytoken tripwire's
 * average lead time over the volumetric channel on scripted attack
 * timelines) rather than a live-computed figure, since there is no
 * live-replay equivalent of that measurement to compute per session —
 * the tooltip says so rather than implying otherwise.
 */

const TRIPWIRE_LEAD_TIME_SEC = 58.4;
const TRIPWIRE_LEAD_TIME_TOOLTIP =
  "Measured on scripted attack timelines (docs/DETECTION_STUDY.md), not this live session specifically -- the honeytoken tripwire's average lead time over the volumetric channel reaching the same compromise.";

const RISK_INDEX_TOOLTIP =
  "risk = clamp(0-100) of Σ over UNACKNOWLEDGED alerts of (severity_weight × asset_criticality), normalised against a configured presentation-scale constant -- not a calibrated probability. Falls when alerts are acknowledged.";

const ALERTS_SUPPRESSED_TOOLTIP =
  "Volumetric anomalies detected, scored, persisted, and broadcast, but deliberately not paged -- the unsupervised channel measures ~0.02 precision on real replayed traffic. Cumulative since the backend last restarted.";

// Simple stroke-only line icons, matching SeverityGlyph's "geometric
// glyph, no emoji" convention rather than reaching for emoji characters.
function ActivityIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true">
      <path d="M2 10h3l2-6 4 12 2-6h5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function AlertIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true">
      <path d="M10 2.5 18 17H2z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
      <path d="M10 8v4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="10" cy="14.2" r="0.9" fill="currentColor" />
    </svg>
  );
}
function ShieldIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true">
      <path d="M10 2.5 17 5v5.2c0 4-3 6.9-7 7.3-4-.4-7-3.3-7-7.3V5z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}
function ClockIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true">
      <circle cx="10" cy="10" r="7.5" stroke="currentColor" strokeWidth="1.6" />
      <path d="M10 5.5V10l3 2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

interface MetricCardProps {
  icon: "activity" | "alert" | "shield" | "clock";
  label: string;
  value: string;
  unit?: string;
  detail?: string;
  detailTone?: "critical" | "warning" | "normal" | "text";
  tone?: "accent" | "critical" | "warning" | "normal" | "text";
  title?: string;
}

const TONE_BG: Record<string, string> = {
  accent: "bg-accent/10 text-accent",
  critical: "bg-sev-critical/10 text-sev-critical",
  warning: "bg-sev-warning/10 text-sev-warning",
  normal: "bg-sev-normal/10 text-sev-normal",
  text: "border border-glass-border-strong bg-glass-raised text-text-dim",
};

const DETAIL_TEXT: Record<string, string> = {
  critical: "text-sev-critical",
  warning: "text-sev-warning",
  normal: "text-sev-normal",
  text: "text-text-mute",
};

const ICON: Record<MetricCardProps["icon"], () => ReactElement> = {
  activity: ActivityIcon,
  alert: AlertIcon,
  shield: ShieldIcon,
  clock: ClockIcon,
};

function MetricCard({ icon, label, value, unit, detail, detailTone = "text", tone = "text", title }: MetricCardProps) {
  const Icon = ICON[icon];
  return (
    <div className="glass-panel flex flex-1 items-center gap-3 p-4" title={title}>
      <span
        className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full ${TONE_BG[tone]}`}
        aria-hidden="true"
      >
        <Icon />
      </span>
      <div className="flex min-w-0 flex-col">
        <span className="text-[11px] font-medium uppercase tracking-[0.06em] text-text-mute">
          {label}
        </span>
        <span className="font-mono text-2xl font-semibold leading-tight tabular-nums text-text">
          {value}
          {unit && <span className="ml-1 text-sm font-medium text-text-mute">{unit}</span>}
        </span>
        {detail && (
          <span className={`text-xs font-medium ${DETAIL_TEXT[detailTone]}`}>{detail}</span>
        )}
      </div>
    </div>
  );
}

export function MetricsStrip() {
  const { eventsPerSecond, alertCount } = useStream();
  const { stats } = useConnection();

  const riskIndex = stats?.risk_index ?? null;
  const riskTone: MetricCardProps["tone"] =
    riskIndex === null ? "text" : riskIndex >= 50 ? "critical" : riskIndex > 0 ? "warning" : "normal";
  const riskDetailTone: MetricCardProps["detailTone"] =
    riskIndex === null ? "text" : riskIndex >= 50 ? "critical" : riskIndex > 0 ? "warning" : "normal";
  const riskLabel = riskIndex === null ? "No basis yet" : riskIndex >= 50 ? "High" : riskIndex > 0 ? "Elevated" : "Nominal";

  // `stats.alerts.unacknowledged` (durable, from the alerts TABLE) is the
  // headline number here, not `useStream()`'s `alertCount` (a live-
  // session-arrival counter that resets to 0 on every reconnect and
  // reads as a confusing "0 active alerts" the moment a tab reconnects
  // while real unacknowledged alerts sit in history — the same figure
  // this console already treats as durable everywhere else, e.g.
  // AlertsRail's REST-loaded list). `alertCount` still appears, in the
  // detail line, labelled for what it actually is.
  const unacknowledged = stats?.alerts.unacknowledged ?? null;
  const suppressed = stats?.ingest.alerts_suppressed;

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <MetricCard
        icon="activity"
        label="Events / sec"
        value={String(eventsPerSecond)}
        tone="accent"
        detail="live replay throughput"
      />
      <MetricCard
        icon="alert"
        label="Active Alerts"
        value={unacknowledged === null ? "—" : String(unacknowledged)}
        tone={unacknowledged === null ? "text" : unacknowledged > 0 ? "critical" : "normal"}
        detail={`${alertCount} received this session${suppressed !== undefined ? ` · ${suppressed} suppressed` : ""}`}
        detailTone={unacknowledged !== null && unacknowledged > 0 ? "critical" : "text"}
        title={ALERTS_SUPPRESSED_TOOLTIP}
      />
      <MetricCard
        icon="shield"
        label="System Risk"
        value={riskIndex === null ? "—" : String(riskIndex)}
        unit={riskIndex === null ? undefined : "/100"}
        tone={riskTone}
        detail={riskLabel}
        detailTone={riskDetailTone}
        title={RISK_INDEX_TOOLTIP}
      />
      <MetricCard
        icon="clock"
        label="Mean Lead Time"
        value={TRIPWIRE_LEAD_TIME_SEC.toFixed(1)}
        unit="s"
        tone="text"
        detail="tripwire advantage"
        title={TRIPWIRE_LEAD_TIME_TOOLTIP}
      />
    </div>
  );
}
