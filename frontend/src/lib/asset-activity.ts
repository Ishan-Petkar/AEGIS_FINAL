/**
 * src/lib/asset-activity.ts — recency-windowed severity for curated
 * topology nodes (Ticket #11 §4 "Anomaly pulse").
 *
 * Mirrors the pulse-window mechanic in `cluster-graph.ts`'s
 * `ClusterAggregator` (see that file's docstring for why a high-water
 * mark diff, not a full-buffer rescan, is used): each stream event whose
 * `source_asset` / `destination_asset` names a curated topology node
 * marks that node's last-seen anomaly/tripwire timestamp; `snapshot()`
 * turns those timestamps into a transient severity that decays after
 * `pulseWindowMs`, which is what makes the pulse a *pulse* rather than a
 * permanent badge.
 *
 * K8 means this will almost always report nothing for ambient replay
 * traffic (real IPs don't resolve to curated asset names — see
 * `cic_ids_adapter`/`AssetRegistry`), which is expected and correct: it
 * only lights up for Ticket #13's scripted-attack injections, which name
 * curated assets directly.
 */

import type { EventEnvelopeData } from "./types";

interface AssetActivity {
  lastAnomalyAt: number;
  lastTripwireAt: number;
}

export class AssetActivityTracker {
  private readonly activity = new Map<string, AssetActivity>();
  private highWaterId: number | null = null;

  private touch(assetName: string, anomaly: boolean, tripwire: boolean, now: number) {
    let a = this.activity.get(assetName);
    if (!a) {
      a = { lastAnomalyAt: 0, lastTripwireAt: 0 };
      this.activity.set(assetName, a);
    }
    if (tripwire) a.lastTripwireAt = now;
    else if (anomaly) a.lastAnomalyAt = now;
  }

  /** `knownAssets` bounds tracking to actual curated node names — never track `Unresolved_*` noise. */
  ingest(events: EventEnvelopeData[], knownAssets: ReadonlySet<string>): void {
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
    const now = Date.now();
    for (const e of fresh) {
      if (knownAssets.has(e.source_asset)) {
        this.touch(e.source_asset, e.is_anomaly, e.tripwire_fired, now);
      }
      if (knownAssets.has(e.destination_asset)) {
        this.touch(e.destination_asset, e.is_anomaly, e.tripwire_fired, now);
      }
    }
    this.highWaterId = newestSeen;
  }

  /** Curated asset names that have appeared (as source or destination) in the stream at least once this session. */
  touchedAssetNames(): string[] {
    return [...this.activity.keys()];
  }

  severityOf(assetName: string, pulseWindowMs = 3000): "normal" | "warning" | "critical" {
    const a = this.activity.get(assetName);
    if (!a) return "normal";
    const now = Date.now();
    if (now - a.lastTripwireAt <= pulseWindowMs) return "critical";
    if (now - a.lastAnomalyAt <= pulseWindowMs) return "warning";
    return "normal";
  }
}
