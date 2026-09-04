"use client";

import { useView } from "@/lib/view-context";
import { MonitoringHeader } from "./monitoring/MonitoringHeader";
import { MonitoringDashboard } from "./monitoring/MonitoringDashboard";
import { AppHeader } from "@/components/AppHeader";
import { TelemetryRail } from "@/components/TelemetryRail";
import { GraphPanel } from "@/components/GraphPanel";
import { AlertsRail } from "@/components/AlertsRail";
import { MetricsStrip } from "@/components/MetricsStrip";
import { DetectionPreventionPanel } from "@/components/DetectionPreventionPanel";
import { ImpactSummaryCard, DetectorSignalsCard } from "@/components/ImpactAndDetectors";
import { RiskTrendChart } from "@/components/RiskTrendChart";
import { SectorHealthStrip } from "@/components/SectorHealthStrip";

export function ClientViewSwitch() {
  const { viewMode } = useView();

  if (viewMode === "monitoring") {
    return (
      <div className="flex flex-col xl:h-full xl:overflow-hidden">
        <MonitoringHeader />
        <MonitoringDashboard />
      </div>
    );
  }

  // Technical view (completely preserved and untouched)
  return (
    <div className="flex flex-col xl:h-full xl:overflow-hidden">
      <AppHeader />
      <main className="flex flex-col gap-3 p-3 xl:min-h-0 xl:flex-1 xl:flex-row">
        <TelemetryRail />
        <div className="flex min-h-0 flex-1 flex-col gap-3 xl:overflow-y-auto">
          <MetricsStrip />
          <div className="flex flex-col gap-3 lg:flex-row xl:h-[56vh] xl:min-h-[460px]">
            <GraphPanel />
            <AlertsRail />
          </div>
          <DetectionPreventionPanel />
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <ImpactSummaryCard />
            <DetectorSignalsCard />
          </div>
          <RiskTrendChart />
        </div>
      </main>
      <div className="px-3 pb-3">
        <SectorHealthStrip />
      </div>
    </div>
  );
}
