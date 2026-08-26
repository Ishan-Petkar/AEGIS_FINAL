import { AppHeader } from "@/components/AppHeader";
import { TelemetryRail } from "@/components/TelemetryRail";
import { GraphPanel } from "@/components/GraphPanel";
import { AlertsRail } from "@/components/AlertsRail";
import { SectorHealthStrip } from "@/components/SectorHealthStrip";

// Console redesign (docs/PHASE5_CONSOLE_REDESIGN_PLAN.md §2, D-R1):
// rebalanced from DESIGN_CONSOLE.md §5's original telemetry-340/graph-672/
// alerts-380 split — the graph (the product's actual differentiator, per
// that doc's own §5 rationale) got under half the width. Telemetry narrows
// to ~280px (rows are `time · src→dst · glyph`; they still fit), alerts to
// ~340px, and the graph takes the remainder as the hero region. A new
// full-width sector health strip sits below all three. Below 1280px the
// alerts rail drops beneath the graph; below 900px everything stacks — the
// console targets desktop but must not break on narrower viewports.
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
      <div className="px-3 pb-3">
        <SectorHealthStrip />
      </div>
    </div>
  );
}
