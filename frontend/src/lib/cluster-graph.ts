/**
 * src/lib/cluster-graph.ts — `/24` observed-traffic clustering (Ticket
 * #11, D11-2).
 *
 * K8 (measured, see docs/PHASE5_TICKET11_PLAN.md §2): 0 of 20,000 real
 * friday-morning source IPs resolve to a curated `DEPENDENCY_GRAPH` node
 * (`AssetRegistry.resolve()` returns `Unresolved_<ip>`) because real
 * CIC-IDS2017 hosts are `192.168.10.x` while every curated asset is
 * `10.0.1.x`. Rather than resolving events into fake positions on the
 * curated topology, this module aggregates the raw `source_ip` /
 * `destination_ip` on every event into `/24` buckets — an honest second
 * layer ("observed traffic") that is NOT joined to the curated layer by
 * any invented edge (D11-1).
 *
 * `ClusterAggregator` is a plain class, not a hook: it owns incremental
 * Maps (`subnets`, `edges`) that must survive across renders without
 * being recreated, and must be updated by *diffing* the event id high
 * water mark rather than reprocessing the full (bounded, 200-entry)
 * `events` buffer on every tick — reprocessing the buffer would make
 * counts drop as old events scroll out of it, which is not what "flow
 * count" should mean for a running session. Callers own the throttling
 * cadence (see `CityGraph.tsx`, which mirrors Ticket #10's ~100ms
 * `TelemetryRail` pattern) — this class does no timing of its own.
 */

import type { EventEnvelopeData } from "./types";

/** Cap on rendered `/24` cluster nodes (D11-2) — the rest roll into `other`. */
export const CLUSTER_CAP = 24;

/** Stable node id for the rollup bucket. */
export const OTHER_CLUSTER_ID = "cluster:other";

const IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.\d{1,3}$/;

/** `192.168.10.5` -> `192.168.10.0/24`. Returns null for non-IPv4 strings. */
export function subnetOf(ip: string): string | null {
  const m = IPV4_RE.exec(ip);
  if (!m) return null;
  return `${m[1]}.${m[2]}.${m[3]}.0/24`;
}

export interface ClusterStat {
  cidr: string;
  count: number;
  anomalyCount: number;
  tripwireCount: number;
  /** `Date.now()` (wall clock, not dataset virtual time) of last anomaly/tripwire seen — drives the pulse window. */
  lastAnomalyAt: number;
  lastTripwireAt: number;
}

export interface ClusterSnapshotNode {
  id: string;
  cidr: string;
  /** True for the synthetic rollup bucket. */
  isOther: boolean;
  count: number;
  anomalyCount: number;
  tripwireCount: number;
  /** Number of individual `/24`s folded into this node (>1 only for `other`). */
  rolledUpCount: number;
  /** Severity within the recent pulse window (see `snapshot`'s `pulseWindowMs`), not cumulative. */
  pulseSeverity: "normal" | "warning" | "critical";
}

export interface ClusterSnapshotLink {
  source: string;
  target: string;
  count: number;
}

export interface ClusterSnapshot {
  nodes: ClusterSnapshotNode[];
  links: ClusterSnapshotLink[];
  /** Total distinct `/24` subnets observed this session (before capping). */
  totalSubnets: number;
  /** Total events folded into the aggregator so far. */
  totalEvents: number;
}

/**
 * Incrementally aggregates stream events into `/24` cluster stats. Call
 * `ingest()` with the full current `events` buffer on each throttled
 * tick — it internally tracks the highest event id already processed and
 * only folds in genuinely new events, so re-passing the same (or a
 * shrunk) buffer is a cheap no-op rather than a re-aggregation.
 */
export class ClusterAggregator {
  private readonly subnets = new Map<string, ClusterStat>();
  // Keyed `${srcCidr}|${dstCidr}`, undirected pair order not normalized —
  // direction matters for "observed flow" edges.
  private readonly edges = new Map<string, ClusterSnapshotLink>();
  private highWaterId: number | null = null;
  private totalEvents = 0;

  private bump(cidr: string, anomaly: boolean, tripwire: boolean, now: number) {
    let stat = this.subnets.get(cidr);
    if (!stat) {
      stat = {
        cidr,
        count: 0,
        anomalyCount: 0,
        tripwireCount: 0,
        lastAnomalyAt: 0,
        lastTripwireAt: 0,
      };
      this.subnets.set(cidr, stat);
    }
    stat.count += 1;
    if (tripwire) {
      stat.tripwireCount += 1;
      stat.lastTripwireAt = now;
    } else if (anomaly) {
      stat.anomalyCount += 1;
      stat.lastAnomalyAt = now;
    }
  }

  /**
   * Folds newly-seen events (by id, strictly greater than the last
   * processed id) into the running aggregates. `events` is expected
   * newest-first (the shape `useEventStream` returns) and bounded, per
   * the module docstring above.
   */
  ingest(events: EventEnvelopeData[]): void {
    if (events.length === 0) return;
    // Newest-first input; walk oldest-to-newest of the *new* slice so
    // high-water-mark bookkeeping stays simple.
    let newestSeen = this.highWaterId;
    const fresh: EventEnvelopeData[] = [];
    for (const e of events) {
      if (this.highWaterId == null || e.id > this.highWaterId) {
        fresh.push(e);
        if (newestSeen == null || e.id > newestSeen) newestSeen = e.id;
      }
    }
    if (fresh.length === 0) return;
    fresh.reverse(); // oldest -> newest
    const now = Date.now();
    for (const e of fresh) {
      const srcCidr = subnetOf(e.source_ip);
      const dstCidr = subnetOf(e.destination_ip);
      if (srcCidr) this.bump(srcCidr, e.is_anomaly, e.tripwire_fired, now);
      if (dstCidr && dstCidr !== srcCidr) this.bump(dstCidr, e.is_anomaly, e.tripwire_fired, now);
      if (srcCidr && dstCidr && srcCidr !== dstCidr) {
        const key = `${srcCidr}|${dstCidr}`;
        const existing = this.edges.get(key);
        if (existing) existing.count += 1;
        else this.edges.set(key, { source: srcCidr, target: dstCidr, count: 1 });
      }
      this.totalEvents += 1;
    }
    this.highWaterId = newestSeen;
  }

  /**
   * Builds the capped, render-ready snapshot: top `CLUSTER_CAP` subnets
   * by flow count, everything else rolled into one `other` node with its
   * own aggregate count. Edges are remapped through the same cap/other
   * mapping and summed, so an edge touching a rolled-up subnet still
   * shows up (pointing at `other`) rather than silently vanishing.
   */
  snapshot(cap: number = CLUSTER_CAP, pulseWindowMs = 3000): ClusterSnapshot {
    const now = Date.now();
    const pulseOf = (s: ClusterStat): "normal" | "warning" | "critical" => {
      if (now - s.lastTripwireAt <= pulseWindowMs) return "critical";
      if (now - s.lastAnomalyAt <= pulseWindowMs) return "warning";
      return "normal";
    };

    const all = [...this.subnets.values()].sort((a, b) => b.count - a.count);
    const top = all.slice(0, cap);
    const rest = all.slice(cap);

    const topIds = new Set(top.map((s) => s.cidr));
    const nodes: ClusterSnapshotNode[] = top.map((s) => ({
      id: s.cidr,
      cidr: s.cidr,
      isOther: false,
      count: s.count,
      anomalyCount: s.anomalyCount,
      tripwireCount: s.tripwireCount,
      rolledUpCount: 1,
      pulseSeverity: pulseOf(s),
    }));

    if (rest.length > 0) {
      const other = rest.reduce(
        (acc, s) => ({
          count: acc.count + s.count,
          anomalyCount: acc.anomalyCount + s.anomalyCount,
          tripwireCount: acc.tripwireCount + s.tripwireCount,
        }),
        { count: 0, anomalyCount: 0, tripwireCount: 0 }
      );
      const otherPulse = rest.some((s) => now - s.lastTripwireAt <= pulseWindowMs)
        ? "critical"
        : rest.some((s) => now - s.lastAnomalyAt <= pulseWindowMs)
          ? "warning"
          : "normal";
      nodes.push({
        id: OTHER_CLUSTER_ID,
        cidr: `${rest.length} other /24s`,
        isOther: true,
        count: other.count,
        anomalyCount: other.anomalyCount,
        tripwireCount: other.tripwireCount,
        rolledUpCount: rest.length,
        pulseSeverity: otherPulse,
      });
    }

    const mapId = (cidr: string): string => (topIds.has(cidr) ? cidr : OTHER_CLUSTER_ID);
    const linkAgg = new Map<string, ClusterSnapshotLink>();
    for (const l of this.edges.values()) {
      const source = mapId(l.source);
      const target = mapId(l.target);
      if (source === target) continue; // both rolled into `other` — skip self-loop
      const key = `${source}|${target}`;
      const existing = linkAgg.get(key);
      if (existing) existing.count += l.count;
      else linkAgg.set(key, { source, target, count: l.count });
    }

    return {
      nodes,
      links: [...linkAgg.values()],
      totalSubnets: all.length,
      totalEvents: this.totalEvents,
    };
  }
}

/** Formats a flow count with real CIDR form, e.g. `192.168.10.0/24 ×1,284`. */
export function formatClusterLabel(node: ClusterSnapshotNode): string {
  const count = node.count.toLocaleString("en-US");
  if (node.isOther) return `${node.rolledUpCount} other /24s ×${count}`;
  return `${node.cidr} ×${count}`;
}
