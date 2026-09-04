import { AppHeader } from "@/components/AppHeader";
import { TelemetryRail } from "@/components/TelemetryRail";
import { GraphPanel } from "@/components/GraphPanel";
import { AlertsRail } from "@/components/AlertsRail";
import { MetricsStrip } from "@/components/MetricsStrip";
import { DetectionPreventionPanel } from "@/components/DetectionPreventionPanel";
import { ImpactSummaryCard, DetectorSignalsCard } from "@/components/ImpactAndDetectors";
import { RiskTrendChart } from "@/components/RiskTrendChart";
import { SectorHealthStrip } from "@/components/SectorHealthStrip";

// Console redesign — light-theme dashboard pass. Restructured from the
// prior single-row (Telemetry | Graph | Alerts | IPS) layout into a
// persistent full-height telemetry rail on the left and a stacked,
// scrollable column on the right: headline metrics, then the graph +
// active-incidents row, then the detailed detection/prevention panel
// (the operator-requested "detailed panel for injections and
// preventions"), then impact/detector summary cards, then a risk-trend
// chart. Every panel keeps its previous data source and behaviour —
// this pass only changes layout and visual theme, not functionality.
export default function Home() {
  return (
    <div className="flex flex-col xl:h-full xl:overflow-hidden">
      <AppHeader />
      <main className="flex flex-col gap-3 p-3 xl:min-h-0 xl:flex-1 xl:flex-row">
        <TelemetryRail />
        <div className="flex min-h-0 flex-1 flex-col gap-3 xl:overflow-y-auto">
          <MetricsStrip />
          {/* A DEFINITE height at `xl`, not `flex-1`. The right column is
              `xl:overflow-y-auto` — a scrolling document region, not a
              viewport-fitted grid — so there is no definite height above
              this row for a `flex-1` child to resolve against. Without
              this, `GraphPanel`'s `xl:h-auto` (GraphPanel.tsx) had nothing
              to stretch to and the canvas ended up sized by whatever
              `AlertsRail` happened to be tall, i.e. by the alert count;
              `AlertsRail`'s own `overflow-y-auto` never engaged either and
              its 100 cards grew the page instead of scrolling. Both panels
              size against this one number now. */}
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
