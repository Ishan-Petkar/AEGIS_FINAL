"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Panel } from "@/components/Panel";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";
import { getAlerts } from "@/lib/api";
import { useThemeColors } from "@/lib/theme-tokens";
import type { AlertEnvelopeData, AlertOut } from "@/lib/types";

interface AlertRecord {
  id: number;
  ts: string;
  severity: string;
}

function fromEnvelope(a: AlertEnvelopeData): AlertRecord {
  return { id: a.id, ts: a.ts, severity: a.severity };
}

function fromAlertOut(a: AlertOut): AlertRecord {
  return { id: a.id, ts: a.ts, severity: a.severity };
}

interface HourBucket {
  hourLabel: string;
  critical: number;
  warning: number;
  normal: number;
  total: number;
}

export function MonitoringTrendChart() {
  const colors = useThemeColors();
  const { reconnectEpoch } = useConnection();
  const { alerts: liveAlerts } = useStream();
  const [restAlerts, setRestAlerts] = useState<AlertRecord[]>([]);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const resp = await getAlerts({ limit: 100 });
        if (cancelled) return;
        setRestAlerts(resp.alerts.map(fromAlertOut));
      } catch {
        // Fallback to live stream
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [reconnectEpoch]);

  const allAlerts = useMemo(() => {
    const map = new Map<number, AlertRecord>();
    for (const a of restAlerts) map.set(a.id, a);
    for (const a of liveAlerts) {
      if (!map.has(a.id)) map.set(a.id, fromEnvelope(a));
    }
    return Array.from(map.values());
  }, [restAlerts, liveAlerts]);

  // Aggregate into 24 hourly buckets
  const buckets = useMemo(() => {
    const now = Date.now();
    const HOUR_MS = 60 * 60 * 1000;
    const result: HourBucket[] = [];

    // Create 24 hours back to now
    for (let i = 23; i >= 0; i--) {
      const bucketTime = new Date(now - i * HOUR_MS);
      const hourStr = bucketTime.getHours().toString().padStart(2, "0") + ":00";
      result.push({
        hourLabel: hourStr,
        critical: 0,
        warning: 0,
        normal: 0,
        total: 0,
      });
    }

    const cutoff = now - 24 * HOUR_MS;
    for (const alert of allAlerts) {
      const ts = Date.parse(alert.ts);
      if (Number.isNaN(ts) || ts < cutoff || ts > now) continue;
      const hoursAgo = Math.floor((now - ts) / HOUR_MS);
      const index = 23 - Math.min(23, Math.max(0, hoursAgo));
      if (index >= 0 && index < 24) {
        if (alert.severity === "critical") {
          result[index].critical++;
        } else if (alert.severity === "warning") {
          result[index].warning++;
        } else {
          result[index].normal++;
        }
        result[index].total++;
      }
    }

    return result;
  }, [allAlerts]);

  const totalAlertsCount = allAlerts.length;

  // Render to canvas
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Handle high-DPI displays
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    const width = rect.width;
    const height = rect.height;

    ctx.clearRect(0, 0, width, height);

    const padLeft = 28;
    const padRight = 12;
    const padTop = 14;
    const padBottom = 26;

    const chartW = width - padLeft - padRight;
    const chartH = height - padTop - padBottom;

    // Max bucket value for scaling (at least 5 for pleasant visual scale)
    const maxVal = Math.max(5, ...buckets.map((b) => b.total));

    // Draw horizontal grid lines
    const gridTicks = [0, Math.round(maxVal / 2), maxVal];
    ctx.strokeStyle = colors.glassBorder || "#e6ded0";
    ctx.lineWidth = 1;
    ctx.fillStyle = colors.textMute || "#7d7566";
    ctx.font = "10px monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    for (const tick of gridTicks) {
      const y = padTop + chartH - (tick / maxVal) * chartH;
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(width - padRight, y);
      ctx.stroke();

      ctx.fillText(tick.toString(), padLeft - 6, y);
    }

    // Draw stacked bars
    const barCount = buckets.length;
    const step = chartW / barCount;
    const barWidth = Math.max(3, step * 0.65);

    buckets.forEach((b, i) => {
      const x = padLeft + i * step + (step - barWidth) / 2;
      let currentY = padTop + chartH;

      // Draw Normal segment
      if (b.normal > 0) {
        const segH = (b.normal / maxVal) * chartH;
        ctx.fillStyle = colors.sevNormal || "#3a7d44";
        ctx.fillRect(x, currentY - segH, barWidth, segH);
        currentY -= segH;
      }

      // Draw Warning segment
      if (b.warning > 0) {
        const segH = (b.warning / maxVal) * chartH;
        ctx.fillStyle = colors.sevWarning || "#b9790f";
        ctx.fillRect(x, currentY - segH, barWidth, segH);
        currentY -= segH;
      }

      // Draw Critical segment
      if (b.critical > 0) {
        const segH = (b.critical / maxVal) * chartH;
        ctx.fillStyle = colors.sevCritical || "#c22f26";
        ctx.fillRect(x, currentY - segH, barWidth, segH);
        currentY -= segH;
      }

      // If bucket has 0 alerts, draw subtle baseline indicator
      if (b.total === 0) {
        ctx.fillStyle = colors.glassBorderStrong || "#c9c1b1";
        ctx.fillRect(x, padTop + chartH - 2, barWidth, 2);
      }
    });

    // Time labels (every 4 hours: 0, 4, 8, 12, 16, 20, 23)
    ctx.fillStyle = colors.textMute || "#7d7566";
    ctx.font = "10px monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";

    [0, 4, 8, 12, 16, 20, 23].forEach((idx) => {
      if (buckets[idx]) {
        const x = padLeft + idx * step + step / 2;
        ctx.fillText(buckets[idx].hourLabel, x, padTop + chartH + 6);
      }
    });
  }, [buckets, colors]);

  return (
    <Panel
      label="Alert Trend (Last 24 Hours)"
      action={
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 text-[10px] text-text-mute">
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-sev-critical inline-block" />
              High
            </span>
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-sev-warning inline-block" />
              Medium
            </span>
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-sev-normal inline-block" />
              Low
            </span>
          </div>
          <span className="font-mono text-xs font-semibold text-text">
            Total: {totalAlertsCount}
          </span>
        </div>
      }
      className="min-h-[340px] xl:min-h-[370px]"
    >
      <div className="relative h-64 md:h-72 w-full pt-2">
        <canvas
          ref={canvasRef}
          className="h-full w-full block"
          style={{ width: "100%", height: "100%" }}
        />
      </div>
    </Panel>
  );
}
