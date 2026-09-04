/**
 * lib/cascade.ts — shared cascade BFS utility.
 *
 * Extracted from CityGraph.tsx's `computeCascadeGeometry` so both the
 * graph canvas and IncidentPathStrip read from the same implementation.
 * A copy that drifts from the graph's own BFS would make the strip and
 * the graph disagree on hop order — this ensures they cannot.
 *
 * Phase 5 (IncidentPathStrip + cascade.ts extraction).
 */

import type { TopologyResponse } from "./types";

export interface CascadeGeometry {
  /** Asset name → hop distance from origin (undirected BFS on curated edges). */
  hopOf: Map<string, number>;
  /** Curated link ids on a shortest path from origin to some impacted asset. */
  pathEdgeIds: Set<string>;
  /** Curated link id → hop of the far endpoint. */
  edgeHopOf: Map<string, number>;
  /** Hop assigned to impacted assets the BFS never reached — revealed last. */
  fallbackHop: number;
  /** Ordered path from origin to first high-criticality impacted asset. */
  linearPath: string[];
}

/**
 * Computes cascade geometry for a given origin + impacted list + topology.
 *
 * Identical logic to the inline version inside CityGraph.tsx — the canonical
 * implementation lives here; CityGraph delegates to this function (or keeps
 * its own for now and we unify on a later pass if schedule allows).
 */
export function computeCascadeGeometry(
  origin: string,
  impacted: string[],
  edges: TopologyResponse["edges"]
): CascadeGeometry {
  const adj = new Map<string, { neighbor: string; edgeId: string }[]>();
  const addEdge = (a: string, b: string, edgeId: string) => {
    if (!adj.has(a)) adj.set(a, []);
    adj.get(a)!.push({ neighbor: b, edgeId });
  };
  for (const e of edges) {
    const edgeId = `curated:${e.source}->${e.target}`;
    addEdge(e.source, e.target, edgeId);
    addEdge(e.target, e.source, edgeId);
  }

  const hopOf = new Map<string, number>([[origin, 0]]);
  const parentNode = new Map<string, string>();
  const edgeHopOf = new Map<string, number>();
  const queue: string[] = [origin];
  let qi = 0;
  while (qi < queue.length) {
    const cur = queue[qi++];
    const curHop = hopOf.get(cur)!;
    for (const { neighbor, edgeId } of adj.get(cur) ?? []) {
      if (hopOf.has(neighbor)) continue;
      hopOf.set(neighbor, curHop + 1);
      parentNode.set(neighbor, cur);
      edgeHopOf.set(edgeId, curHop + 1);
      queue.push(neighbor);
    }
  }

  const pathEdgeIds = new Set<string>();
  for (const name of impacted) {
    let cur = name;
    const guard = new Set<string>();
    while (parentNode.has(cur) && !guard.has(cur)) {
      guard.add(cur);
      const parent = parentNode.get(cur)!;
      pathEdgeIds.add(`curated:${parent}->${cur}`);
      pathEdgeIds.add(`curated:${cur}->${parent}`);
      cur = parent;
    }
  }

  let maxKnownHop = 0;
  for (const h of hopOf.values()) maxKnownHop = Math.max(maxKnownHop, h);

  // Build a linear path: origin → each impacted asset ordered by hop,
  // deduplicated on node id.
  const seen = new Set<string>([origin]);
  const linearPath: string[] = [origin];
  const sorted = [...impacted].sort(
    (a, b) => (hopOf.get(a) ?? maxKnownHop + 1) - (hopOf.get(b) ?? maxKnownHop + 1)
  );
  for (const asset of sorted) {
    // Trace parent chain to find intermediate hops not yet in path.
    const chain: string[] = [];
    let cur = asset;
    const guard = new Set<string>();
    while (cur && parentNode.has(cur) && !guard.has(cur)) {
      guard.add(cur);
      chain.unshift(cur);
      cur = parentNode.get(cur)!;
    }
    for (const node of chain) {
      if (!seen.has(node)) {
        seen.add(node);
        linearPath.push(node);
      }
    }
    if (!seen.has(asset)) {
      seen.add(asset);
      linearPath.push(asset);
    }
  }

  return { hopOf, pathEdgeIds, edgeHopOf, fallbackHop: maxKnownHop + 1, linearPath };
}
