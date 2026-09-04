"use client";

import { useMemo } from "react";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";
import { useTopology } from "@/lib/topology-context";
import {
  backendSectorToServiceName,
  humanizeAssetName,
} from "@/lib/monitoring-translator";
import { buildSectorByName } from "@/lib/sectors";

export function MonitoringHero() {
  const { stats } = useConnection();
  const { alerts } = useStream();
  const { state: topoState } = useTopology();

  const sectorByName = useMemo(() => {
    if (topoState.kind === "loaded") {
      return buildSectorByName(topoState.data.nodes);
    }
    return new Map<string, string | null>();
  }, [topoState]);

  // Find any active (unacknowledged) alerts
  const unackedAlerts = useMemo(() => {
    return alerts.filter((a) => !a.acknowledged);
  }, [alerts]);

  const unackedCount = stats?.alerts?.unacknowledged ?? unackedAlerts.length;
  const hasActiveIncident = unackedCount > 0 || unackedAlerts.length > 0;

  // Derive worst severity
  const worstSeverity = useMemo(() => {
    if (!hasActiveIncident) return "normal";
    if (unackedAlerts.some((a) => a.severity === "critical")) return "critical";
    if (unackedAlerts.some((a) => a.severity === "warning")) return "warning";
    return "warning";
  }, [hasActiveIncident, unackedAlerts]);

  // Find affected services for subheadline
  const affectedServiceName = useMemo(() => {
    if (!hasActiveIncident || unackedAlerts.length === 0) return null;
    const first = unackedAlerts[0];
    const sec = sectorByName.get(first.asset);
    return backendSectorToServiceName(sec);
  }, [hasActiveIncident, unackedAlerts, sectorByName]);

  return (
    <div className="glass-panel relative overflow-hidden rounded-[var(--radius-panel)] border border-glass-border py-3 px-4 md:py-3.5 md:px-5">
      <div className="relative z-10 flex flex-col md:flex-row md:items-center md:justify-between gap-3">
        <div className="flex items-center gap-3.5">
          {/* Status Shield Icon */}
          <div
            className={`flex h-10 w-10 md:h-11 md:w-11 shrink-0 items-center justify-center rounded-xl transition-colors duration-300 ${
              !hasActiveIncident
                ? "bg-sev-normal/10 text-sev-normal border border-sev-normal/25"
                : worstSeverity === "critical"
                  ? "bg-sev-critical/10 text-sev-critical border border-sev-critical/25"
                  : "bg-sev-warning/10 text-sev-warning border border-sev-warning/25"
            }`}
          >
            {!hasActiveIncident ? (
              <svg
                className="h-5 w-5 md:h-6 md:w-6"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                <path d="m9 12 2 2 4-4" />
              </svg>
            ) : (
              <svg
                className="h-5 w-5 md:h-6 md:w-6 animate-pulse"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                <line x1="12" y1="8" x2="12" y2="12" />
                <line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
            )}
          </div>

          {/* Titles */}
          <div>
            <h1 className="text-base md:text-lg font-bold tracking-tight text-text">
              {!hasActiveIncident
                ? "No Active Attacks"
                : unackedCount === 1
                  ? "Active Security Incident"
                  : `${unackedCount} Active Security Incidents`}
            </h1>
            <p className="text-xs text-text-dim">
              {!hasActiveIncident
                ? "All systems are operating normally across the municipal grid."
                : affectedServiceName
                  ? `Elevated activity detected in ${affectedServiceName}. Automated safeguards active.`
                  : "Anomalous activity detected. Automated defense safeguards active."}
            </p>
          </div>
        </div>

        {/* Right Status Badge */}
        <div className="flex items-center self-start md:self-auto">
          {!hasActiveIncident ? (
            <div className="flex items-center gap-2 rounded-full border border-sev-normal/30 bg-sev-normal/10 px-3 py-1 text-xs font-semibold text-sev-normal shadow-sm">
              <span className="h-1.5 w-1.5 rounded-full bg-sev-normal animate-pulse" />
              <span>City infrastructure is secure</span>
              <span className="text-text-mute ml-0.5" aria-hidden="true">
                →
              </span>
            </div>
          ) : (
            <div
              className={`flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-semibold shadow-sm ${
                worstSeverity === "critical"
                  ? "border-sev-critical/30 bg-sev-critical/10 text-sev-critical"
                  : "border-sev-warning/30 bg-sev-warning/10 text-sev-warning"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  worstSeverity === "critical"
                    ? "bg-sev-critical animate-ping"
                    : "bg-sev-warning animate-pulse"
                }`}
              />
              <span>
                {worstSeverity === "critical"
                  ? "High Severity Anomaly"
                  : "Service Warning Active"}
              </span>
              <span className="opacity-75 ml-0.5" aria-hidden="true">
                →
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
