"use client";

import { useEffect, useMemo, useState } from "react";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";
import { useTopology } from "@/lib/topology-context";
import { getAlerts } from "@/lib/api";
import { buildSectorByName } from "@/lib/sectors";
import type { AlertEnvelopeData, AlertOut } from "@/lib/types";
import {
  backendSectorToServiceName,
  formatAlertTime,
  humanizeAssetName,
} from "@/lib/monitoring-translator";

interface AlertRecord {
  id: number;
  ts: string;
  severity: string;
  asset: string;
  title: string;
  acknowledged: boolean;
}

function fromEnvelope(a: AlertEnvelopeData): AlertRecord {
  return {
    id: a.id,
    ts: a.ts,
    severity: a.severity,
    asset: a.asset,
    title: a.title,
    acknowledged: a.acknowledged,
  };
}

function fromAlertOut(a: AlertOut): AlertRecord {
  return {
    id: a.id,
    ts: a.ts,
    severity: a.severity,
    asset: a.asset,
    title: a.title,
    acknowledged: a.acknowledged,
  };
}

export function MonitoringIncidentBanner() {
  const { reconnectEpoch } = useConnection();
  const { alerts: liveAlerts } = useStream();
  const { state: topoState } = useTopology();
  const [restAlerts, setRestAlerts] = useState<AlertRecord[]>([]);

  const sectorByName = useMemo(() => {
    if (topoState.kind === "loaded") {
      return buildSectorByName(topoState.data.nodes);
    }
    return new Map<string, string | null>();
  }, [topoState]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const resp = await getAlerts({ limit: 10 });
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

  const latestAlert = useMemo(() => {
    const map = new Map<number, AlertRecord>();
    for (const a of restAlerts) map.set(a.id, a);
    for (const a of liveAlerts) {
      if (!map.has(a.id)) map.set(a.id, fromEnvelope(a));
    }
    const list = Array.from(map.values()).sort((a, b) => {
      const ta = Date.parse(a.ts);
      const tb = Date.parse(b.ts);
      if (ta !== tb) return tb - ta;
      return b.id - a.id;
    });
    return list[0] ?? null;
  }, [restAlerts, liveAlerts]);

  const isResolved = !latestAlert || latestAlert.acknowledged;
  const isCritical = latestAlert && !latestAlert.acknowledged && latestAlert.severity === "critical";

  const humanAsset = latestAlert ? humanizeAssetName(latestAlert.asset) : null;
  const serviceName = latestAlert ? backendSectorToServiceName(sectorByName.get(latestAlert.asset)) : null;
  const timeFormatted = latestAlert ? formatAlertTime(latestAlert.ts) : null;

  return (
    <div className="glass-panel flex flex-col justify-between rounded-[var(--radius-panel)] border border-glass-border p-4 gap-3 flex-1 min-h-[160px] xl:min-h-[175px]">
      {/* Header Row: Icon + State Label + Status Pill */}
      <div className="flex items-center justify-between gap-2 border-b border-glass-border pb-2.5">
        <div className="flex items-center gap-2.5">
          <div
            className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${
              isResolved
                ? "bg-sev-normal/10 text-sev-normal"
                : isCritical
                  ? "bg-sev-critical/10 text-sev-critical animate-pulse"
                  : "bg-sev-warning/10 text-sev-warning"
            }`}
          >
            {isResolved ? (
              <svg
                className="h-4 w-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <polyline points="20 6 9 17 4 12" />
              </svg>
            ) : (
              <svg
                className="h-4 w-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
            )}
          </div>

          <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim">
            {isResolved ? "Last Alert Resolved" : "Active Incident Status"}
          </h2>
        </div>

        <span
          className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${
            isResolved
              ? "bg-sev-normal/10 text-sev-normal border-sev-normal/25"
              : isCritical
                ? "bg-sev-critical/10 text-sev-critical border-sev-critical/25"
                : "bg-sev-warning/10 text-sev-warning border-sev-warning/25"
          }`}
        >
          {isResolved ? "Normal" : isCritical ? "Critical" : "Warning"}
        </span>
      </div>

      {/* Body: Incident details */}
      <p className="text-xs text-text-dim leading-relaxed">
        {!latestAlert ? (
          "All municipal systems are operating normally. No active incidents requiring operator intervention."
        ) : isResolved ? (
          <>
            The alert on <strong className="text-text font-medium">{humanAsset}</strong> at {timeFormatted} has been resolved and verified.
          </>
        ) : (
          <>
            Investigating anomalous activity on <strong className="text-text font-medium">{humanAsset}</strong> ({serviceName}). Automated mitigation engaged.
          </>
        )}
      </p>

      {/* Footer Row: Timestamp + Action Link */}
      <div className="flex items-center justify-between pt-2 border-t border-glass-border text-xs">
        <span className="font-mono text-[11px] text-text-mute">
          {timeFormatted ? `Timestamp: ${timeFormatted}` : "Continuous monitoring active"}
        </span>

        <button
          type="button"
          className="text-xs font-semibold text-accent hover:underline flex items-center gap-1 transition-colors"
        >
          <span>View Details</span>
          <span aria-hidden="true">→</span>
        </button>
      </div>
    </div>
  );
}
