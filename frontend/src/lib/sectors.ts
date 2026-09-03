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

// Synthetic sector for nodes `GET /api/topology` reports with `sector:
// null` — the Purdue-zone gateways and the synthesized `City_Grid` node,
// which `config.SMART_CITY_ASSETS` doesn't assign to any real sector
// because they aren't owned by one. Earlier this meant these ~5 nodes
// were dropped entirely from the sector-aggregated (compact) view: no
// chip, no aggregate bubble, and — since `buildDisplayTopology`'s edge
// remap has no display node to point them at — every edge touching them
// vanished too, invisible even with every real sector expanded by hand.
// That silently hid the actual access-control layer a cascade routes
// through, which is exactly the part of the graph an operator most needs
// to see. Treating "core" as a real, tenth sector (grouped, chipped,
// expandable exactly like Finance or Energy) fixes that: these nodes are
// still findable and clickable in the compact view, one gateway/City_Grid
// bubble to open instead of the hub's own always-on singularity.
export const CORE_SECTOR = "core";

// Fixed, deterministic order — re-renders never reshuffle sectors. Matches
// every real `sector` value `src/config.py` assigns except "operations"
// (the hub's own sector, which is never aggregated into a chip/node — the
// hub is drawn/summarised on its own) plus the synthetic `CORE_SECTOR`
// appended last for the gateways/City_Grid fallback above.
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
  CORE_SECTOR,
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
  [CORE_SECTOR]: "Infrastructure",
};

export function sectorLabel(sector: string): string {
  return SECTOR_LABELS[sector] ?? sector;
}

/** Stable id for a sector's aggregate graph node — namespaced so it can never collide with a real asset name. */
export function sectorNodeId(sector: string): string {
  return `sector:${sector}`;
}

/**
 * Real (non-hub) curated nodes grouped by sector. A node with no real
 * `sector` (gateways, City_Grid) falls into `CORE_SECTOR` rather than
 * being dropped — see that constant's docstring for why omitting them
 * used to hide real graph structure from the compact view.
 */
export function groupNodesBySector(
  nodes: TopologyResponse["nodes"]
): Map<string, TopologyResponse["nodes"]> {
  const bySector = new Map<string, TopologyResponse["nodes"]>();
  for (const n of nodes) {
    if (n.name === HUB_ASSET_NAME) continue;
    const sector = n.sector ?? CORE_SECTOR;
    const list = bySector.get(sector);
    if (list) list.push(n);
    else bySector.set(sector, [n]);
  }
  return bySector;
}

/**
 * Asset name -> sector, for O(1) lookups. A gateway/City_Grid name
 * resolves to `CORE_SECTOR` (never `null`) — the same fallback
 * `groupNodesBySector` applies, kept in sync here so a caller checking
 * "does this asset belong to a sector" (e.g. the cascade auto-focus
 * effect in `CityGraph`) can rely on one consistent answer instead of
 * treating gateways as sector-less everywhere except the graph's own
 * grouping.
 */
export function buildSectorByName(nodes: TopologyResponse["nodes"]): Map<string, string> {
  return new Map(nodes.map((n) => [n.name, n.sector ?? CORE_SECTOR] as const));
}
