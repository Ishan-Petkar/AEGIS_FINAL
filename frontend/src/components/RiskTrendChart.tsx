"use client";

import { useEffect, useRef, useState } from "react";
import { Panel } from "./Panel";
import { useConnection } from "@/lib/connection-context";

// RiskTrendChart — console redesign, light-theme dashboard pass.
//
// Real, incrementally-collected data, not a fabricated demo curve:
// `useConnection()` already polls `GET /api/stats` every 5s (see
// connection-context.tsx's POLL_INTERVAL_MS) for the header's own Risk
// chip. This component appends `stats.risk_index` to a capped client-
// side rolling buffer each time a NEW poll actually lands, and plots
// whatever real history has accumulated since this tab opened — an
// honest "since you've been watching" trend, not a fabricated 30-minute
// backfill the client has no way to know. A short buffer (few samples,
// e.g. right after page load) is shown as-is, not stretched or padded.

const WINDOW_MS = 30 * 60 * 1000; // 30 min, matching the panel's own label
const MAX_SAMPLES = Math.ceil(WINDOW_MS / 5000) + 5;

interface Sample {
  t: number;
  v: number;
}

function formatClock(t: number): string {
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function RiskTrendChart() {
  const { stats } = useConnection();
  const [samples, setSamples] = useState<Sample[]>([]);
  const lastRecordedRef = useRef<number | null>(null);

  useEffect(() => {
    if (stats === null) return;
    // `stats` is a fresh object every successful poll (connection-context
    // constructs a new StatsResponse each time), so a reference change
    // IS a new poll landing -- no need for a separate timestamp field on
    // the response itself.
    const now = Date.now();
    if (lastRecordedRef.current !== null && now - lastRecordedRef.current < 2000) return;
    lastRecordedRef.current = now;
    setSamples((prev) => {
      const next = [...prev, { t: now, v: stats.risk_index }];
      const cutoff = now - WINDOW_MS;
      const trimmed = next.filter((s) => s.t >= cutoff);
      return trimmed.length > MAX_SAMPLES ? trimmed.slice(trimmed.length - MAX_SAMPLES) : trimmed;
    });
  }, [stats]);

  return (
    <Panel label="Risk Trend (session)" className="min-h-[220px]">
      {samples.length < 2 ? (
        <p className="flex h-full items-center justify-center text-center text-sm text-text-mute">
          Collecting samples — the risk index is polled every 5s. Chart fills in as history accumulates.
        </p>
      ) : (
        <Chart samples={samples} />
      )}
    </Panel>
  );
}

function Chart({ samples }: { samples: Sample[] }) {
  const w = 800;
  const h = 180;
  const padL = 32;
  const padB = 20;
  const padT = 10;
  const plotW = w - padL - 8;
  const plotH = h - padT - padB;

  const tMin = samples[0].t;
  const tMax = samples[samples.length - 1].t;
  const tSpan = Math.max(1, tMax - tMin);

  const x = (t: number) => padL + ((t - tMin) / tSpan) * plotW;
  const y = (v: number) => padT + plotH - (Math.max(0, Math.min(100, v)) / 100) * plotH;

  const linePoints = samples.map((s) => `${x(s.t).toFixed(1)},${y(s.v).toFixed(1)}`).join(" ");
  const areaPoints = `${padL},${padT + plotH} ${linePoints} ${x(tMax).toFixed(1)},${padT + plotH}`;

  const last = samples[samples.length - 1];
  const ticks = [0, 50, 100];
  const timeTicks = [samples[0], samples[Math.floor(samples.length / 2)], last];

  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label="Risk index over the current session">
      <defs>
        <linearGradient id="riskFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--sev-critical)" stopOpacity="0.18" />
          <stop offset="100%" stopColor="var(--sev-critical)" stopOpacity="0" />
        </linearGradient>
      </defs>

      {ticks.map((t) => (
        <g key={t}>
          <line x1={padL} x2={w - 8} y1={y(t)} y2={y(t)} stroke="var(--glass-border)" strokeWidth="1" />
          <text x={4} y={y(t) + 3} fontSize="10" fill="var(--text-mute)">
            {t}
          </text>
        </g>
      ))}

      <polygon points={areaPoints} fill="url(#riskFill)" />
      <polyline points={linePoints} fill="none" stroke="var(--sev-critical)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(last.t)} cy={y(last.v)} r="3.5" fill="var(--sev-critical)" />

      {timeTicks.map((s, i) => (
        <text
          key={i}
          x={x(s.t)}
          y={h - 4}
          fontSize="10"
          fill="var(--text-mute)"
          textAnchor={i === 0 ? "start" : i === timeTicks.length - 1 ? "end" : "middle"}
        >
          {formatClock(s.t)}
        </text>
      ))}
    </svg>
  );
}
