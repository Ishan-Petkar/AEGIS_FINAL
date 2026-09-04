"use client";

import { AppHeader } from "@/components/AppHeader";
import { TelemetryRail } from "@/components/TelemetryRail";
import { GraphPanel } from "@/components/GraphPanel";
import { AlertsRail } from "@/components/AlertsRail";
import { MetricsStrip } from "@/components/MetricsStrip";
import { DetectionPreventionPanel } from "@/components/DetectionPreventionPanel";
import { ImpactSummaryCard, DetectorSignalsCard } from "@/components/ImpactAndDetectors";
import { RiskTrendChart } from "@/components/RiskTrendChart";
import { SectorHealthStrip } from "@/components/SectorHealthStrip";
import { useViewMode } from "@/lib/view-mode-context";
import { NTVStatusBanner } from "@/components/ntv/NTVStatusBanner";
import { NTVSectorGrid } from "@/components/ntv/NTVSectorGrid";
import { NTVAlertTrend } from "@/components/ntv/NTVAlertTrend";
import { NTVRecentAlerts } from "@/components/ntv/NTVRecentAlerts";
import { NTVLastResolved } from "@/components/ntv/NTVLastResolved";
import { TVBottomStrip } from "@/components/TVBottomStrip";

export default function Home() {
  const { viewMode } = useViewMode();

  return (
    <div className="flex flex-col h-dvh overflow-hidden bg-ground text-text selection:bg-accent selection:text-white">
      <AppHeader />
      
      {viewMode === "non-technical" ? (
        <main className="flex flex-col gap-3 p-3 min-h-0 flex-1 overflow-hidden">
          {/* Top: Status Banner */}
          <div className="shrink-0">
            <NTVStatusBanner />
          </div>
          
          {/* Middle row: Sector Grid and Alert Trend */}
          <div className="flex gap-3 flex-1 min-h-0">
            <div className="w-2/3 flex flex-col h-full min-h-0">
              <NTVSectorGrid />
            </div>
            <div className="w-1/3 flex flex-col h-full min-h-0">
              <NTVAlertTrend />
            </div>
          </div>

          {/* Bottom row: Graph and Last Resolved */}
          <div className="flex gap-3 shrink-0 h-[320px]">
            <div className="w-2/3 flex flex-col h-full min-h-0">
              <GraphPanel />
            </div>
            <div className="w-1/3 flex flex-col h-full min-h-0">
              <NTVLastResolved />
            </div>
          </div>
        </main>
      ) : (
        <>
          <main className="flex flex-col gap-3 p-3 min-h-0 flex-1 xl:flex-row">
            <TelemetryRail />
            <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden">
              <div className="flex flex-col gap-3 flex-1 min-h-0 lg:flex-row">
                <GraphPanel />
                <AlertsRail />
              </div>
              <TVBottomStrip />
            </div>
          </main>
          <div className="px-3 pb-3 shrink-0">
            <SectorHealthStrip />
          </div>
        </>
      )}
    </div>
  );
}
