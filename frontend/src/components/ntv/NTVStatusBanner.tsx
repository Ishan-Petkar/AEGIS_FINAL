"use client";

import { useStream } from "@/lib/stream-context";
import { useConnection } from "@/lib/connection-context";
import { Panel } from "../Panel";

export function NTVStatusBanner() {
  const { alertCount } = useStream();
  const { stats } = useConnection();

  const unacknowledged = stats?.alerts.unacknowledged ?? null;
  const hasAlerts = unacknowledged !== null && unacknowledged > 0;

  return (
    <Panel
      label=""
      className={`h-[80px] !p-6 ${hasAlerts ? "bg-sev-critical/5 border-sev-critical/20" : "bg-sev-normal/5 border-sev-normal/20"}`}
    >
      <div className="flex items-center gap-4 h-full">
        {/* Status Icon */}
        <div className="shrink-0">
          <div className={`flex h-12 w-12 items-center justify-center rounded-xl border ${hasAlerts ? "bg-sev-critical/10 border-sev-critical/20 text-sev-critical" : "bg-white border-glass-border shadow-sm text-sev-normal"}`}>
            {hasAlerts ? (
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                <line x1="12" y1="9" x2="12" y2="13"/>
                <line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
            ) : (
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                <path d="M9 12l2 2 4-4"/>
              </svg>
            )}
          </div>
        </div>

        {/* Text */}
        <div className="flex flex-col">
          <h2 className="text-xl font-bold text-text mb-0.5">
            {hasAlerts ? "Active Attack Detected" : "No Active Attacks"}
          </h2>
          <p className="text-sm text-text-dim">
            {hasAlerts 
              ? `${unacknowledged} municipal systems are currently exhibiting anomalous behavior requiring attention.`
              : "All systems are operating normally across the municipal grid."}
          </p>
        </div>

        {/* Right Status Pill */}
        <div className="ml-auto shrink-0">
          <div className={`flex items-center gap-2 rounded-full border px-4 py-1.5 text-xs font-semibold ${hasAlerts ? "bg-sev-critical/10 border-sev-critical/20 text-sev-critical" : "bg-sev-normal/10 border-sev-normal/20 text-sev-normal"}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${hasAlerts ? "bg-sev-critical" : "bg-sev-normal"}`}></span>
            {hasAlerts ? "Threats detected →" : "City infrastructure is secure →"}
          </div>
        </div>
      </div>
    </Panel>
  );
}
