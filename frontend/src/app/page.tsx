import { AppHeader } from "@/components/AppHeader";
import { TelemetryRail } from "@/components/TelemetryRail";
import { GraphPanel } from "@/components/GraphPanel";
import { AlertsRail } from "@/components/AlertsRail";

// Three-region body per DESIGN_CONSOLE.md §5: telemetry rail (fixed
// width) | city infrastructure graph (hero, flex) | alerts rail (fixed
// width). Below 1280px the alerts rail drops beneath the graph; below
// 900px all three stack — the console targets desktop but must not break
// on narrower viewports.
export default function Home() {
  return (
    <div className="flex min-h-screen flex-col">
      <AppHeader />
      <main className="flex min-h-0 flex-1 flex-col gap-3 p-3 xl:flex-row">
        <TelemetryRail />
        <GraphPanel />
        <AlertsRail />
      </main>
    </div>
  );
}
