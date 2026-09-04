"use client";

import { MonitoringHero } from "./MonitoringHero";
import { MonitoringSectorGrid } from "./MonitoringSectorGrid";
import { MonitoringRecentAlerts } from "./MonitoringRecentAlerts";
import { MonitoringTrendChart } from "./MonitoringTrendChart";
import { MonitoringIncidentBanner } from "./MonitoringIncidentBanner";

export function MonitoringDashboard() {
  return (
    <main className="flex-1 overflow-y-auto min-h-0 p-3 flex flex-col gap-3 w-full">
      {/* 1. Slim Safety Status Hero Banner */}
      <MonitoringHero />

      {/* 2. Main 2-Column Operational Grid stretching full height */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 flex-1 min-h-0 items-stretch">
        {/* Left Column: 11 Sector Cards + Open All Card + Recent Alerts Table */}
        <div className="lg:col-span-7 xl:col-span-8 flex flex-col gap-3 min-h-0 flex-1">
          <MonitoringSectorGrid />
          <div className="flex-1 flex flex-col min-h-0">
            <MonitoringRecentAlerts />
          </div>
        </div>

        {/* Right Column: Alert Trend (Top) + Incident Status Box (Below) */}
        <div className="lg:col-span-5 xl:col-span-4 flex flex-col gap-3 min-h-0 flex-1 justify-between">
          <MonitoringTrendChart />
          <div className="flex-1 flex flex-col justify-end min-h-0">
            <MonitoringIncidentBanner />
          </div>
        </div>
      </div>
    </main>
  );
}
