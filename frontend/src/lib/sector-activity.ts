/**
 * src/lib/sector-activity.ts — cumulative per-sector activity for the
 * sector health strip (docs/PHASE5_CONSOLE_REDESIGN_PLAN.md §4, D-R3).
 *
 * Deliberately a SEPARATE tracker from `AssetActivityTracker`
 * (`@/lib/asset-activity.ts`), even though both read the same event
 * stream, because the two answer different questions with different
 * decay rules:
 *
 *   `AssetActivityTracker` — "is this asset pulsing *right now*?" A
 *   short (few-second) recency window, used to badge the live graph so a
 *   pulse actually reads as a pulse.
 *
 *   `SectorActivityTracker` (this file) — "what has this sector seen
 *   THIS SESSION?" Monotonic: `eventCount` only grows and
 *   `worstSeverityEver` only ever escalates (normal -> warning ->
 *   critical), never resets or decays. A strip chip that flickered on the
 *   same 3s window as the graph's pulse would be far less useful for an
 *   operator scanning "which sectors have seen trouble" — the whole point
 *   of a always-visible summary strip is that it doesn't require having
 *   watched the graph continuously.
 *
 * Mirrors `AssetActivityTracker`'s high-water-mark diffing (ingest only
 * events newer than the highest id already seen) rather than rescanning
 * the bounded event buffer every tick, for the same reason: a full
 * rescan would make counts drop as old events scroll out of the
 * (200-entry) buffer, which is not what "this session" should mean.
 */

import type { EventEnvelopeData } from "./types";

export type Severity = "normal" | "warning" | "critical";

const SEVERITY_RANK: Record<Severity, number> = { normal: 0, warning: 1, critical: 2 };

interface SectorActivity {
  eventCount: number;
  worstSeverityEver: Severity;
}

export class SectorActivityTracker {
  private readonly activity = new Map<string, SectorActivity>();
  private highWaterId: number | null = null;

  private touch(sector: string, severity: Severity) {
    let a = this.activity.get(sector);
    if (!a) {
      a = { eventCount: 0, worstSeverityEver: "normal" };
      this.activity.set(sector, a);
    }
    a.eventCount += 1;
    if (SEVERITY_RANK[severity] > SEVERITY_RANK[a.worstSeverityEver]) {
      a.worstSeverityEver = severity;
    }
  }

  /**
   * `sectorByAssetName` maps a curated asset's real name to its sector
   * (`null`/missing for gateways, City_Grid, or an unresolved asset —
   * never counted). An event whose source and destination both resolve
   * to the same sector is still counted once for that sector (matching
   * `AssetActivityTracker`'s per-asset "touched" semantics, not a
   * double-count of one flow).
   */
  ingest(events: EventEnvelopeData[], sectorByAssetName: ReadonlyMap<string, string | null>): void {
    if (events.length === 0) return;
    let newestSeen = this.highWaterId;
    const fresh: EventEnvelopeData[] = [];
    for (const e of events) {
      if (this.highWaterId == null || e.id > this.highWaterId) {
        fresh.push(e);
        if (newestSeen == null || e.id > newestSeen) newestSeen = e.id;
      }
    }
    if (fresh.length === 0) return;
    for (const e of fresh) {
      const severity: Severity = e.tripwire_fired ? "critical" : e.is_anomaly ? "warning" : "normal";
      const touched = new Set<string>();
      const srcSector = sectorByAssetName.get(e.source_asset);
      if (srcSector) touched.add(srcSector);
      const dstSector = sectorByAssetName.get(e.destination_asset);
      if (dstSector) touched.add(dstSector);
      for (const sector of touched) this.touch(sector, severity);
    }
    this.highWaterId = newestSeen;
  }

  eventCountOf(sector: string): number {
    return this.activity.get(sector)?.eventCount ?? 0;
  }

  worstSeverityOf(sector: string): Severity {
    return this.activity.get(sector)?.worstSeverityEver ?? "normal";
  }
}
