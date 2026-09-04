"use client";

import { useEffect, useState } from "react";
import { Panel } from "../Panel";
import { getAlerts } from "@/lib/api";
import type { AlertOut } from "@/lib/types";

export function NTVLastResolved() {
  const [lastResolved, setLastResolved] = useState<AlertOut | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const resp = await getAlerts({ limit: 1, acknowledged: true });
        if (!cancelled && resp.alerts.length > 0) {
          setLastResolved(resp.alerts[0]);
        }
      } catch (err) {
        // Fallback to null
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel label="" className="h-full w-full">
      <div className="flex flex-col justify-between h-full p-2">
        <div>
          <div className="flex justify-between items-center mb-6">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim">
              LAST ALERT RESOLVED
            </h2>
            <div className="rounded-full border border-glass-border-strong px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-text-dim bg-white">
              Normal
            </div>
          </div>
          
          <p className="text-[13px] text-text leading-relaxed">
            {lastResolved ? (
              <>Incident on <strong>{lastResolved.asset}</strong> resolved at {new Date(lastResolved.acknowledged_at || "").toLocaleTimeString()}. All municipal systems returned to nominal operation.</>
            ) : (
              "All municipal systems are operating normally. No active incidents requiring operator intervention."
            )}
          </p>
        </div>

        <div className="flex justify-between items-center mt-6 pt-4 border-t border-glass-border">
          <div className="text-[10px] font-mono text-text-mute">Continuous monitoring active</div>
        </div>
      </div>
    </Panel>
  );
}
