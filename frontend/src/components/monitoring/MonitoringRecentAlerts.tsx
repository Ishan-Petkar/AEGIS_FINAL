"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/Panel";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";
import { useTopology } from "@/lib/topology-context";
import { getAlerts } from "@/lib/api";
import { buildSectorByName } from "@/lib/sectors";
import type { AlertEnvelopeData, AlertOut } from "@/lib/types";
import {
  backendSectorToServiceName,
  deriveAlertStatus,
  formatAlertTime,
  humanizeAssetName,
  mapSeverityToLabel,
  translateAlertTitle,
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

function mergeInto(map: Map<number, AlertRecord>, incoming: AlertRecord[]): void {
  for (const record of incoming) {
    if (!map.has(record.id)) {
      map.set(record.id, record);
    }
  }
}

function sortNewestFirst(records: AlertRecord[]): AlertRecord[] {
  return [...records].sort((a, b) => {
    const ta = Date.parse(a.ts);
    const tb = Date.parse(b.ts);
    if (ta !== tb) return tb - ta;
    return b.id - a.id;
  });
}

export function MonitoringRecentAlerts() {
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
        const resp = await getAlerts({ limit: 50 });
        if (cancelled) return;
        setRestAlerts(resp.alerts.map(fromAlertOut));
      } catch {
        // Handled silently; live stream fallback
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [reconnectEpoch]);

  const sortedAlerts = useMemo(() => {
    const map = new Map<number, AlertRecord>();
    mergeInto(map, restAlerts);
    mergeInto(map, liveAlerts.map(fromEnvelope));
    return sortNewestFirst(Array.from(map.values()));
  }, [restAlerts, liveAlerts]);

  // Take top 6 for the monitoring view
  const topAlerts = useMemo(() => sortedAlerts.slice(0, 6), [sortedAlerts]);

  return (
    <Panel
      label="Recent Alerts"
      action={
        <span className="text-[11px] font-medium text-accent hover:underline cursor-pointer">
          View All →
        </span>
      }
      className="h-full flex-1 flex flex-col min-h-[420px] xl:min-h-[460px]"
      bodyClassName="flex-1 flex flex-col min-h-0"
    >
      {topAlerts.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center text-center p-6 min-h-[260px]">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-sev-normal/10 text-sev-normal mb-2">
            <svg
              className="h-5 w-5"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="20 6 9 17 4 12" />
            </svg>
          </div>
          <p className="text-sm font-medium text-text">No active alerts</p>
          <p className="text-xs text-text-mute mt-1 max-w-sm">
            All city services are operating within baseline thresholds. No security interventions currently required.
          </p>
        </div>
      ) : (
        <div className="flex-1 overflow-x-auto overflow-y-auto min-h-0">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="border-b border-glass-border text-[10px] font-semibold uppercase tracking-wider text-text-mute">
                <th className="py-2.5 px-3">Time</th>
                <th className="py-2.5 px-3">Alert</th>
                <th className="py-2.5 px-3">Affected System</th>
                <th className="py-2.5 px-3">Severity</th>
                <th className="py-2.5 px-3">Status</th>
                <th className="py-2.5 px-2 text-right"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-glass-border">
              {topAlerts.map((alert) => {
                const status = deriveAlertStatus(alert);
                const sevLabel = mapSeverityToLabel(alert.severity);
                const sector = sectorByName.get(alert.asset);
                const serviceName = backendSectorToServiceName(sector);
                const translatedTitle = translateAlertTitle(alert.title);
                const humanizedAsset = humanizeAssetName(alert.asset);

                return (
                  <tr
                    key={alert.id}
                    className="hover:bg-glass-raised/60 transition-colors duration-150"
                  >
                    {/* Time */}
                    <td className="py-3 px-3 font-mono text-[11px] text-text-dim whitespace-nowrap">
                      {formatAlertTime(alert.ts)}
                    </td>

                    {/* Translated Alert Title */}
                    <td className="py-3 px-3 font-medium text-text max-w-xs truncate">
                      {translatedTitle}
                    </td>

                    {/* Affected System */}
                    <td className="py-3 px-3 text-text-dim whitespace-nowrap">
                      <div className="flex flex-col">
                        <span className="font-medium text-text">{humanizedAsset}</span>
                        <span className="text-[10px] text-text-mute">{serviceName}</span>
                      </div>
                    </td>

                    {/* Severity */}
                    <td className="py-3 px-3 whitespace-nowrap">
                      <div className="flex items-center gap-1.5">
                        <span
                          className={`h-2 w-2 rounded-full ${
                            alert.severity === "critical"
                              ? "bg-sev-critical"
                              : alert.severity === "warning"
                                ? "bg-sev-warning"
                                : "bg-sev-normal"
                          }`}
                        />
                        <span
                          className={`text-xs font-medium ${
                            alert.severity === "critical"
                              ? "text-sev-critical"
                              : alert.severity === "warning"
                                ? "text-sev-warning"
                                : "text-text-dim"
                          }`}
                        >
                          {sevLabel}
                        </span>
                      </div>
                    </td>

                    {/* Status badge */}
                    <td className="py-3 px-3 whitespace-nowrap">
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold border ${
                          status === "Resolved"
                            ? "bg-sev-normal/10 text-sev-normal border-sev-normal/25"
                            : status === "Open"
                              ? "bg-sev-critical/10 text-sev-critical border-sev-critical/25"
                              : "bg-sev-warning/10 text-sev-warning border-sev-warning/25"
                        }`}
                      >
                        {status}
                      </span>
                    </td>

                    {/* Chevron */}
                    <td className="py-3 px-2 text-right text-text-mute">
                      <span className="inline-block text-sm" aria-hidden="true">
                        ›
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
