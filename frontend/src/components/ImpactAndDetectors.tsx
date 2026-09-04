"use client";

import { useMemo } from "react";
import { Panel } from "./Panel";
import { buildSectorByName, sectorLabel } from "@/lib/sectors";
import { useStream } from "@/lib/stream-context";
import { useTopology } from "@/lib/topology-context";

// ImpactAndDetectors — console redesign, light-theme dashboard pass.
// Two compact cards reusing data every other panel already has: the
// most recent CII (blast-radius) snapshot broadcast on the live stream,
// and a short rolling window of the live event feed's own per-detector
// signals. Nothing new is fetched from the backend for either card.

function parseImpacted(raw: unknown): { assets: string[]; count: number } {
  if (raw && typeof raw === "object" && "assets" in raw && Array.isArray((raw as { assets: unknown }).assets)) {
    const assets = (raw as { assets: unknown[] }).assets.filter((a): a is string => typeof a === "string");
    return { assets, count: assets.length };
  }
  return { assets: [], count: 0 };
}

function severityLabel(ciiMedian: number): { label: string; tone: string } {
  if (ciiMedian >= 0.2) return { label: "High", tone: "text-sev-critical" };
  if (ciiMedian >= 0.05) return { label: "Moderate", tone: "text-sev-warning" };
  if (ciiMedian > 0) return { label: "Low", tone: "text-sev-normal" };
  return { label: "None", tone: "text-text-mute" };
}

export function ImpactSummaryCard() {
  const { latestCii } = useStream();
  const { state } = useTopology();

  const sectorByName = useMemo(
    () => (state.kind === "loaded" ? buildSectorByName(state.data.nodes) : new Map<string, string>()),
    [state]
  );

  const impacted = latestCii ? parseImpacted(latestCii.impacted) : { assets: [], count: 0 };
  const sectorsAffected = useMemo(() => {
    const set = new Set<string>();
    for (const asset of impacted.assets) {
      const sector = sectorByName.get(asset);
      if (sector) set.add(sector);
    }
    return set;
  }, [impacted.assets, sectorByName]);

  const severity = severityLabel(latestCii?.cii_median ?? 0);

  return (
    <Panel label="Impact Summary" className="min-h-[140px]">
      {!latestCii ? (
        <p className="flex h-full items-center justify-center text-center text-sm text-text-mute">
          No cascading-impact snapshot yet this session.
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Assets Impacted" value={String(impacted.count)} />
          <Stat label="Sectors Affected" value={String(sectorsAffected.size)} />
          <Stat label="CII Score" value={latestCii.cii_median.toFixed(2)} />
          <Stat label="Impact Severity" value={severity.label} valueClassName={severity.tone} />
          <p className="col-span-full font-mono text-[11px] text-text-mute">
            Origin: {latestCii.origin_asset} · median {latestCii.cii_median.toFixed(3)} (p5{" "}
            {latestCii.cii_p5.toFixed(3)} · p95 {latestCii.cii_p95.toFixed(3)})
            {sectorsAffected.size > 0 && (
              <> — {Array.from(sectorsAffected).map(sectorLabel).join(", ")}</>
            )}
          </p>
        </div>
      )}
    </Panel>
  );
}

function Stat({ label, value, valueClassName }: { label: string; value: string; valueClassName?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className={`font-mono text-xl font-semibold tabular-nums ${valueClassName ?? "text-text"}`}>
        {value}
      </span>
      <span className="text-[10px] font-medium uppercase tracking-[0.06em] text-text-mute">{label}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detector Signals — a rolling window of the live feed's own scores,
// labelled by exactly what the wire actually carries (see note on the
// "fusion" card below — there is no standalone Random Forest score on
// the live envelope, only whether it's among the hybrid layer's fired
// detectors, so this card is honest about representing the FUSED signal
// rather than mislabelling it as a pure Random Forest score).
// ---------------------------------------------------------------------------

const WINDOW = 40;

function Sparkline({ values, color }: { values: number[]; color: string }) {
  if (values.length < 2) {
    return <div className="h-8 w-full" />;
  }
  const w = 120;
  const h = 32;
  const max = Math.max(0.01, ...values);
  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = h - (v / max) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height="32" preserveAspectRatio="none" aria-hidden="true">
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export function DetectorSignalsCard() {
  const { events } = useStream();
  const window = events.slice(0, WINDOW);

  const volumetricScores = window.map((e) => e.calibrated_score).reverse();
  const fusionScores = window.map((e) => e.hybrid?.threat_score ?? 0).reverse();
  const tripwireFired = window.some((e) => e.tripwire_fired);

  const latestVolumetric = volumetricScores[volumetricScores.length - 1];
  const latestFusion = fusionScores[fusionScores.length - 1];

  return (
    <Panel label="Detector Signals" className="min-h-[140px]">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div>
          <p className="text-[11px] font-medium text-text-dim">Isolation Forest</p>
          <p className="text-[10px] text-text-mute">Novel Threats</p>
          <Sparkline values={volumetricScores} color="var(--sev-normal)" />
          <p className="font-mono text-lg font-semibold tabular-nums text-text">
            {latestVolumetric !== undefined ? latestVolumetric.toFixed(2) : "—"}
          </p>
        </div>
        <div>
          <p className="text-[11px] font-medium text-text-dim">Fusion Engine</p>
          <p className="text-[10px] text-text-mute">All Channels Combined</p>
          <Sparkline values={fusionScores} color="var(--sev-warning)" />
          <p className="font-mono text-lg font-semibold tabular-nums text-text">
            {latestFusion !== undefined ? latestFusion.toFixed(2) : "—"}
          </p>
        </div>
        <div>
          <p className="text-[11px] font-medium text-text-dim">Deception Tripwire</p>
          <p className="text-[10px] text-text-mute">Recon / Credential</p>
          <div className="h-8" />
          <p className={`font-mono text-lg font-semibold ${tripwireFired ? "text-sev-critical" : "text-sev-normal"}`}>
            {window.length === 0 ? "—" : tripwireFired ? "FIRED" : "Quiet"}
          </p>
        </div>
      </div>
    </Panel>
  );
}
