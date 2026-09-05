"use client";

import { useState } from "react";
import { useStream } from "@/lib/stream-context";
import { useConnection } from "@/lib/connection-context";
import { DetailOverlay } from "./DetailOverlay";
import { DetectionPreventionPanel } from "./DetectionPreventionPanel";
import { ImpactSummaryCard, DetectorSignalsCard } from "./ImpactAndDetectors";
import { RiskTrendChart } from "./RiskTrendChart";

const ACTIVE_STATUSES = new Set(["simulated", "enforced"]);
const ACTIVE_PREVENTION_ACTIONS = new Set(["rate_limit", "block", "quarantine"]);
function isActivePrevention(a: any) {
  return ACTIVE_PREVENTION_ACTIONS.has(a.action) && ACTIVE_STATUSES.has(a.status) && !a.rolled_back_at;
}

function parseImpacted(raw: unknown): { count: number } {
  if (raw && typeof raw === "object" && "assets" in raw && Array.isArray((raw as any).assets)) {
    return { count: (raw as any).assets.length };
  }
  return { count: 0 };
}

function MiniSparkline({ values, color }: { values: number[]; color: string }) {
  if (values.length < 2) return <div className="h-4 w-12" />;
  const w = 48, h = 16;
  const max = Math.max(0.01, ...values);
  const points = values.map((v, i) => `${(i / (values.length - 1) * w).toFixed(1)},${(h - (v / max) * h).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="48" height="16" aria-hidden="true" className="opacity-75">
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

export function TVBottomStrip() {
  const [activeOverlay, setActiveOverlay] = useState<"ips" | "impact" | "detectors" | "risk" | null>(null);
  
  const { ipsActions, latestCii, events, tripwireEverFired } = useStream();
  const { stats } = useConnection();

  // 1. IPS/IDS
  const activeMitigations = ipsActions.filter(isActivePrevention).length;
  
  // 2. Impact
  const impacted = latestCii ? parseImpacted(latestCii.impacted) : { count: 0 };
  const ciiScore = latestCii ? latestCii.cii_median.toFixed(2) : "—";
  
  // 3. Detectors. Tripwire status is session-monotonic (`tripwireEverFired`),
  // not derived from this recent window: at real replay throughput a
  // 40-event window can roll a hit back out within a fraction of a
  // second, which reads as "the tripwire status never updates" even
  // though it was correctly received. See useEventStream's docstring.
  const window = events.slice(0, 40);
  const fusionScores = window.map((e) => e.hybrid?.threat_score ?? 0).reverse();
  const latestFusion = fusionScores[fusionScores.length - 1];

  // 4. Risk
  const riskIndex = stats?.risk_index ?? null;

  return (
    <>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 shrink-0">
        
        {/* Tile 1: IPS/IDS */}
        <div className="glass-panel p-4 flex flex-col justify-between min-h-[110px] group cursor-pointer hover:border-glass-border-strong transition-colors" onClick={() => setActiveOverlay("ips")}>
          <div className="flex justify-between items-start">
            <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-text-dim">IPS / IDS Summary</span>
            <span className="text-[9px] uppercase tracking-wider font-bold text-accent opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
              Details <span aria-hidden="true">▸</span>
            </span>
          </div>
          <div className="flex items-end gap-3 mt-2">
            <div className="font-mono text-3xl font-semibold tabular-nums leading-none">
              {activeMitigations}
            </div>
            <div className="flex flex-col pb-0.5">
              <span className="text-[10px] font-medium text-text-mute uppercase tracking-widest leading-tight">Active</span>
              <span className="text-[10px] font-medium text-text-mute uppercase tracking-widest leading-tight">Mitigations</span>
            </div>
          </div>
        </div>

        {/* Tile 2: Impact */}
        <div className="glass-panel p-4 flex flex-col justify-between min-h-[110px] group cursor-pointer hover:border-glass-border-strong transition-colors" onClick={() => setActiveOverlay("impact")}>
          <div className="flex justify-between items-start">
            <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-text-dim">Impact Summary</span>
            <span className="text-[9px] uppercase tracking-wider font-bold text-accent opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
              Details <span aria-hidden="true">▸</span>
            </span>
          </div>
          <div className="flex items-end gap-3 mt-2">
            <div className={`font-mono text-3xl font-semibold tabular-nums leading-none ${latestCii && latestCii.cii_median >= 0.2 ? 'text-sev-critical' : latestCii && latestCii.cii_median >= 0.05 ? 'text-sev-warning' : 'text-text'}`}>
              {ciiScore}
            </div>
            <div className="flex flex-col pb-0.5">
              <span className="text-[10px] font-medium text-text-mute uppercase tracking-widest leading-tight">CII Score</span>
              <span className="text-[10px] font-medium text-text-mute uppercase tracking-widest leading-tight">{impacted.count} Assets</span>
            </div>
          </div>
        </div>

        {/* Tile 3: Detectors */}
        <div className="glass-panel p-4 flex flex-col justify-between min-h-[110px] group cursor-pointer hover:border-glass-border-strong transition-colors" onClick={() => setActiveOverlay("detectors")}>
          <div className="flex justify-between items-start">
            <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-text-dim">Detector Signals</span>
            <span className="text-[9px] uppercase tracking-wider font-bold text-accent opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
              Details <span aria-hidden="true">▸</span>
            </span>
          </div>
          <div className="grid grid-cols-2 gap-x-2 mt-2">
            <div className="flex flex-col justify-end">
               <span className="text-[9px] text-text-mute uppercase tracking-wider">Fusion</span>
               <div className="flex items-center gap-2 mt-1">
                 <span className="font-mono text-sm">{latestFusion !== undefined ? latestFusion.toFixed(2) : "—"}</span>
                 <MiniSparkline values={fusionScores} color="var(--sev-warning)" />
               </div>
            </div>
            <div className="flex flex-col justify-end">
               <span className="text-[9px] text-text-mute uppercase tracking-wider">Tripwire</span>
               <div className="flex items-center mt-1">
                 <span className={`font-mono text-sm ${tripwireEverFired ? "text-sev-critical font-bold" : "text-sev-normal"}`}>
                   {tripwireEverFired ? "FIRED" : "Quiet"}
                 </span>
               </div>
            </div>
          </div>
        </div>

        {/* Tile 4: Risk */}
        <div className="glass-panel p-4 flex flex-col justify-between min-h-[110px] group cursor-pointer hover:border-glass-border-strong transition-colors" onClick={() => setActiveOverlay("risk")}>
          <div className="flex justify-between items-start">
            <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-text-dim">Risk Trend</span>
            <span className="text-[9px] uppercase tracking-wider font-bold text-accent opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
              Details <span aria-hidden="true">▸</span>
            </span>
          </div>
          <div className="flex items-end gap-3 mt-2">
            <div className={`font-mono text-3xl font-semibold tabular-nums leading-none ${riskIndex && riskIndex >= 50 ? 'text-sev-critical' : riskIndex && riskIndex > 0 ? 'text-sev-warning' : 'text-text'}`}>
              {riskIndex === null ? "—" : String(riskIndex)}
            </div>
            <div className="flex flex-col pb-0.5">
              <span className="text-[10px] font-medium text-text-mute uppercase tracking-widest leading-tight">Index</span>
              <span className="text-[10px] font-medium text-text-mute uppercase tracking-widest leading-tight">/ 100</span>
            </div>
          </div>
        </div>

      </div>

      <DetailOverlay title="Detection & Prevention (IPS / IDS)" open={activeOverlay === "ips"} onClose={() => setActiveOverlay(null)}>
        <DetectionPreventionPanel />
      </DetailOverlay>

      <DetailOverlay title="Impact & Detector Signals" open={activeOverlay === "impact" || activeOverlay === "detectors"} onClose={() => setActiveOverlay(null)}>
        <div className="flex flex-col gap-4">
           <ImpactSummaryCard />
           <DetectorSignalsCard />
        </div>
      </DetailOverlay>

      <DetailOverlay title="Risk Trend" open={activeOverlay === "risk"} onClose={() => setActiveOverlay(null)}>
        <RiskTrendChart />
      </DetailOverlay>
    </>
  );
}
