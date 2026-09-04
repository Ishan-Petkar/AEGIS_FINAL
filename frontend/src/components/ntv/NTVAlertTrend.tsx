"use client";

import { useEffect, useMemo, useState } from "react";
import { useStream } from "@/lib/stream-context";
import { getAlerts } from "@/lib/api";
import type { AlertOut } from "@/lib/types";
import { Panel } from "../Panel";

export function NTVAlertTrend() {
  const { alerts: liveAlerts } = useStream();
  const [restAlerts, setRestAlerts] = useState<AlertOut[]>([]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const resp = await getAlerts({ limit: 100 });
        if (!cancelled) setRestAlerts(resp.alerts);
      } catch (err) {}
    }
    load();
    return () => { cancelled = true; };
  }, []);

  const allAlerts = useMemo(() => {
    const map = new Map<number, any>();
    restAlerts.forEach(a => map.set(a.id, a));
    liveAlerts.forEach(a => map.set(a.id, a));
    return Array.from(map.values()).sort((a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime());
  }, [restAlerts, liveAlerts]);

  const counts = useMemo(() => {
    let high = 0, med = 0, low = 0;
    allAlerts.forEach(a => {
      if (a.severity === "critical") high++;
      else if (a.severity === "warning") med++;
      else low++;
    });
    return { high, med, low, total: allAlerts.length };
  }, [allAlerts]);

  // Create 24 buckets for the bar chart
  const buckets = useMemo(() => {
    const bucketsArray = Array.from({ length: 24 }, () => ({ high: 0, med: 0, low: 0 }));
    if (allAlerts.length === 0) return bucketsArray;
    
    const tMin = new Date(allAlerts[0].ts).getTime();
    const tMax = new Date(allAlerts[allAlerts.length - 1].ts).getTime();
    const span = tMax - tMin || 1;
    
    allAlerts.forEach(a => {
      const t = new Date(a.ts).getTime();
      let bucketIdx = Math.floor(((t - tMin) / span) * 24);
      if (bucketIdx >= 24) bucketIdx = 23;
      
      if (a.severity === "critical") bucketsArray[bucketIdx].high++;
      else if (a.severity === "warning") bucketsArray[bucketIdx].med++;
      else bucketsArray[bucketIdx].low++;
    });
    return bucketsArray;
  }, [allAlerts]);

  return (
    <Panel label="" className="h-full w-full">
      <div className="flex flex-col h-full relative">
        <div className="flex justify-between items-center mb-2">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim">
            ALERT TREND (LAST 24 HOURS)
          </h2>
          <div className="flex gap-3 text-[10px] items-center text-text-mute font-medium uppercase">
            <div className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-sev-critical"></span>High</div>
            <div className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-sev-warning"></span>Medium</div>
            <div className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-sev-normal"></span>Low</div>
            <div className="ml-1 text-text-dim font-bold">Total: {counts.total}</div>
          </div>
        </div>

        <div className="relative flex-1 w-full flex flex-col mt-2">
           {/* Background Grid Lines */}
           <div className="absolute inset-x-0 top-0 bottom-6 flex flex-col justify-between pointer-events-none z-0">
             <div className="border-b border-glass-border/50 w-full flex-1"></div>
             <div className="border-b border-glass-border/50 w-full flex-1"></div>
             <div className="border-b border-glass-border/50 w-full flex-1"></div>
             <div className="border-b border-glass-border w-full"></div>
           </div>

           <div className="relative z-10 flex-1 w-full flex items-end justify-between gap-1 pb-1">
             {allAlerts.length === 0 ? (
               <div className="absolute inset-0 flex items-center justify-center text-sm text-text-mute">Waiting for alert data...</div>
             ) : (
               buckets.map((b, i) => {
                 const total = b.high + b.med + b.low;
                 const maxBucket = Math.max(...buckets.map(bk => bk.high + bk.med + bk.low), 1);
                 const heightPct = Math.max((total / maxBucket) * 100, 0); // 0 if empty
                 
                 return (
                   <div 
                     key={i} 
                     className="flex-1 flex flex-col justify-end gap-[1px] h-full group relative cursor-pointer"
                     title={total > 0 ? `${total} alerts\nHigh: ${b.high}\nMedium: ${b.med}\nLow: ${b.low}` : "No alerts"}
                   >
                      {total === 0 ? (
                        <div className="w-full h-[2px] bg-glass-raised/50 rounded-sm transition-colors group-hover:bg-glass-border" />
                      ) : (
                        <div className="w-full flex flex-col justify-end gap-[1px] transition-opacity group-hover:opacity-80" style={{ height: `${heightPct}%`, minHeight: '4px' }}>
                          {b.high > 0 && <div className="w-full bg-sev-critical rounded-t-[2px]" style={{ flexGrow: b.high }} />}
                          {b.med > 0 && <div className="w-full bg-sev-warning" style={{ flexGrow: b.med, borderRadius: b.high === 0 ? '2px 2px 0 0' : '0' }} />}
                          {b.low > 0 && <div className="w-full bg-sev-normal rounded-b-[2px]" style={{ flexGrow: b.low, borderRadius: (b.high === 0 && b.med === 0) ? '2px 2px 2px 2px' : '0 0 2px 2px' }} />}
                        </div>
                      )}
                   </div>
                 );
               })
             )}
           </div>

           {/* X-Axis Labels */}
           <div className="flex justify-between items-center h-5 mt-1 text-[9px] font-mono text-text-mute">
              <span>24h ago</span>
              <span>12h ago</span>
              <span>Now</span>
           </div>
        </div>
      </div>
    </Panel>
  );
}
