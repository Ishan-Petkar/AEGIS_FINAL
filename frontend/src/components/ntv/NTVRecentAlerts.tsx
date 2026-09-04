"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "../Panel";
import { getAlerts, ApiError, ApiNetworkError } from "@/lib/api";
import { useStream } from "@/lib/stream-context";
import { useViewMode } from "@/lib/view-mode-context";
import type { AlertEnvelopeData, AlertOut } from "@/lib/types";

interface AlertRecord {
  id: number;
  ts: string;
  severity: string;
  asset: string;
  title: string;
}

function fromAlertOut(a: AlertOut): AlertRecord {
  return { id: a.id, ts: typeof a.ts === "string" ? a.ts : String(a.ts), severity: a.severity, asset: a.asset, title: a.title };
}
function fromEnvelope(a: AlertEnvelopeData): AlertRecord {
  return { id: a.id, ts: a.ts, severity: a.severity, asset: a.asset, title: a.title };
}

export function NTVRecentAlerts() {
  const { alerts: liveAlerts } = useStream();
  const { setViewMode } = useViewMode();
  const [restAlerts, setRestAlerts] = useState<AlertRecord[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setIsLoading(true);
      try {
        const resp = await getAlerts({ limit: 10, acknowledged: false });
        if (cancelled) return;
        setRestAlerts(resp.alerts.map(fromAlertOut));
      } catch (err) {
        // Ignore error state in simplified NTV panel, fallback to empty
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  const sorted = useMemo(() => {
    const map = new Map<number, AlertRecord>();
    restAlerts.forEach(a => map.set(a.id, a));
    liveAlerts.forEach(a => { if (!map.has(a.id)) map.set(a.id, fromEnvelope(a)); });
    return Array.from(map.values()).sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts)).slice(0, 5);
  }, [restAlerts, liveAlerts]);

  return (
    <Panel 
      label="RECENT ALERTS"
      action={
        <button 
          onClick={() => setViewMode("technical")}
          className="text-[10px] font-semibold text-accent hover:text-accent-hi uppercase tracking-wider"
        >
          View All →
        </button>
      }
      className="h-full min-h-[300px]"
      bodyClassName="h-full flex flex-col relative"
    >
      {isLoading ? (
        <div className="flex-1 flex items-center justify-center text-sm text-text-dim">Loading alerts...</div>
      ) : sorted.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-center gap-3 p-6">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-sev-normal/10 text-sev-normal mb-2">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M20 6L9 17l-5-5"/>
            </svg>
          </div>
          <h3 className="text-[15px] font-semibold text-text">No active alerts</h3>
          <p className="text-[13px] text-text-mute max-w-sm">
            All city services are operating within baseline thresholds. No security interventions currently required.
          </p>
        </div>
      ) : (
        <ul className="flex flex-col gap-3 overflow-y-auto max-h-[400px]">
          {sorted.map(alert => (
            <li key={alert.id} className={`glass-panel border-l-2 p-3 ${alert.severity === 'critical' ? 'border-l-sev-critical' : 'border-l-sev-warning'}`}>
              <div className="flex items-center gap-2 mb-1">
                <span className={`text-[10px] font-bold uppercase tracking-wider ${alert.severity === 'critical' ? 'text-sev-critical' : 'text-sev-warning'}`}>
                  {alert.severity}
                </span>
                <span className="font-mono text-[10px] text-text-mute ml-auto">{new Date(alert.ts).toLocaleTimeString()}</span>
              </div>
              <div className="text-xs font-semibold text-text mb-1 truncate">{alert.title}</div>
              <div className="font-mono text-[10px] text-text-dim">{alert.asset}</div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
