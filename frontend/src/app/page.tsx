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
  // `xl:h-full` (not a bare `h-full`), matching `body`'s `xl:h-full` in
  // layout.tsx: only at the `xl` breakpoint (the 3-column grid, see
  // `main` below) is this a fixed-viewport console where every panel
  // that needs to scroll (TelemetryRail, AlertsRail, and GraphPanel's
  // ResizeObserver-driven CityGraph) does so internally against a
  // definite ancestor height. Below `xl`, panels stack — `h-full` there
  // would clamp the *whole document* to the viewport with no scrollbar,
  // clipping whatever doesn't fit (see Ticket #11 fix-round HIGH-1).
  // Below `xl` the document is left to scroll normally instead, and
  // GraphPanel gives itself an explicit height directly (h-[420px]) so
  // its canvas still gets a definite size without needing this element,
  // `body`, or `html` to be clamped.
  return (
    <div className="flex flex-col xl:h-full xl:overflow-hidden">
      <AppHeader />
      <main className="flex flex-col gap-3 p-3 xl:min-h-0 xl:flex-1 xl:flex-row">
        <TelemetryRail />
        <GraphPanel />
        <AlertsRail />
      </main>
    </div>
  );
}
