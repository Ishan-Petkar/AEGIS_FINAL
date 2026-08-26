/**
 * src/lib/sectors.ts — shared sector-grouping helpers (console redesign,
 * docs/PHASE5_CONSOLE_REDESIGN_PLAN.md §3, D-R2/D-R3).
 *
 * `GET /api/topology` carries a real `sector` field per node, sourced from
 * `config.SMART_CITY_ASSETS` (the one permitted backend touch this plan
 * allows) — sector membership is derived from real data, never guessed on
 * the frontend. Both `CityGraph` (the sector-aggregated default view) and
 * `SectorHealthStrip` (the health chips) need the exact same grouping, so
 * it lives here once rather than being computed twice and risking the two
 * disagreeing about which assets belong to which sector.
 */

import type { TopologyResponse } from "./types";

export const HUB_ASSET_NAME = "City_Operations_Center";

// Fixed, deterministic order — re-renders never reshuffle sectors. Matches
// every real `sector` value `src/config.py` assigns except "operations"
// (the hub's own sector, which is never aggregated into a chip/node — the
// hub is drawn/summarised on its own).
export const SECTOR_ORDER = [
  "energy",
  "water",
  "transport",
  "public_safety",
  "health",
  "telecom",
  "finance",
  "civic",
  "environment",
  "monitoring",
];

const SECTOR_LABELS: Record<string, string> = {
  energy: "Energy",
  water: "Water",
  transport: "Transport",
  public_safety: "Public Safety",
  health: "Health",
  telecom: "Telecom/IT",
  finance: "Finance",
  civic: "Civic",
  environment: "Environment",
  monitoring: "Monitoring",
};

export function sectorLabel(sector: string): string {
  return SECTOR_LABELS[sector] ?? sector;
}

/** Stable id for a sector's aggregate graph node — namespaced so it can never collide with a real asset name. */
export function sectorNodeId(sector: string): string {
  return `sector:${sector}`;
}

/** Real (non-hub) curated nodes grouped by their real `sector` field. Nodes with no sector (gateways, City_Grid) are omitted. */
export function groupNodesBySector(
  nodes: TopologyResponse["nodes"]
): Map<string, TopologyResponse["nodes"]> {
  const bySector = new Map<string, TopologyResponse["nodes"]>();
  for (const n of nodes) {
    if (n.name === HUB_ASSET_NAME || !n.sector) continue;
    const list = bySector.get(n.sector);
    if (list) list.push(n);
    else bySector.set(n.sector, [n]);
  }
  return bySector;
}

/** Asset name -> sector (including `null` for gateways/City_Grid/hub-less nodes), for O(1) lookups. */
export function buildSectorByName(nodes: TopologyResponse["nodes"]): Map<string, string | null> {
  return new Map(nodes.map((n) => [n.name, n.sector] as const));
}
