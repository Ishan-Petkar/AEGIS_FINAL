"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import { useStream } from "@/lib/stream-context";
import { useThemeColors, useMonoFontFamily, usePrefersReducedMotion } from "@/lib/theme-tokens";
import {
  ClusterAggregator,
  CLUSTER_CAP,
  formatClusterLabel,
  type ClusterSnapshotNode,
} from "@/lib/cluster-graph";
import { AssetActivityTracker } from "@/lib/asset-activity";
import { useGraphFocus } from "@/lib/graph-focus-context";
import {
  CORE_SECTOR,
  HUB_ASSET_NAME,
  SECTOR_ORDER,
  buildSectorByName,
  groupNodesBySector,
  sectorLabel,
  sectorNodeId,
} from "@/lib/sectors";
import type { TopologyResponse } from "@/lib/types";

// Ticket #11 — the real force-directed graph. Two invariants drive every
// decision in this file:
//
// D11-1 (docs/PHASE5_TICKET11_PLAN.md §2): the curated city model (from
// GET /api/topology) and observed `/24` traffic clusters are rendered as
// two layers that are NEVER joined by an edge this component invents.
// K8 measured 0/20,000 real source IPs resolving onto curated assets, so
// for ambient replay traffic they are genuinely disconnected — the
// caption below says so honestly instead of hiding it.
//
// D11-2: cluster aggregation runs off a `ClusterAggregator` (see
// `@/lib/cluster-graph.ts`) fed on the SAME ~100ms throttled cadence
// Ticket #10 uses for the telemetry feed, never per WS message, and caps
// rendered cluster nodes at `CLUSTER_CAP` with the remainder rolled into
// one `other` node. Node/link objects are mutated in place (same object
// identity across ticks) so `react-force-graph` preserves their x/y and
// the simulation never visibly thrashes on an update.

const RENDER_INTERVAL_MS = 100;
const PULSE_WINDOW_MS = 3000;
// Ticket #14 (D14-2): per-hop stagger for the cascade reveal, and how long
// a pulsing ring animates before settling. Not in SETTINGS — this is a
// frontend-only presentation timing, not a domain tunable (the backend
// computes the CII distribution; this file only paces how it is revealed).
const CASCADE_STAGGER_MS = 260;
// Phase 2: passed explicitly to `ForceGraph2D` and used to derive `nodeVal`
// from the same drawn radius `nodeCanvasObject` paints — react-force-graph's
// hit-radius formula is `radius = sqrt(val) * nodeRelSize`, so
// `val = (radius / nodeRelSize)^2` inverts it exactly.
const NODE_REL_SIZE = 4;
// Phase 1 fix: nothing previously reset `cascade` back to null, so the
// origin/impacted rings and lit path persisted for the rest of the
// session — an hour-old attack looked identical to one seconds old.
// Hold the fully-revealed state for this long after the last hop would
// have revealed, then clear.
const CASCADE_HOLD_MS = 9000;

// Ticket #16 FIX round (HIGH-1): the observed `/24` cluster layer is
// confined to a narrow vertical band hugging the right edge of the
// container — a "peripheral band", not the roughly-half-canvas region
// the first pass gave it. `CLUSTER_ZONE_LEFT_FRAC` is the left edge of
// that band (as a fraction of container width); everything left of it,
// out to the container edges, belongs to the curated city. `seedPosition`,
// `computeCuratedLayout`, and `makeClusterConfineForce` all target this
// same boundary so a newly-arriving cluster node seeds inside the band
// and the curated ellipse's radius never crosses into it.
const CLUSTER_ZONE_LEFT_FRAC = 0.84;
const CLUSTER_ZONE_GAP_FRAC = 0.03;

interface CuratedNodeDatum {
  id: string;
  layer: "curated";
  label: string;
  nodeType: string | null;
  criticality: number;
  isGateway: boolean;
  isFinancial: boolean;
  // Console redesign (D-R2): true for a sector-aggregate node (id
  // `sector:<key>`) representing >=1 real curated assets collapsed into
  // one point in the default view. `memberCount` is always 1 for a real
  // (non-aggregate) node.
  isAggregate: boolean;
  memberCount: number;
  pulseSeverity: "normal" | "warning" | "critical";
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  // Ticket #14 (D14-1): the curated layer is pinned once, deterministically
  // (see `computeCuratedLayout`) — fx/fy make d3-force treat position as
  // fixed regardless of any force (charge from arriving clusters included).
  fx?: number;
  fy?: number;
  // Ticket #16 FIX round (HIGH-1): the label anchor's offset from the
  // node's own (x, y), in the same units. `labelDy`'s sign still decides
  // the text baseline (negative draws above the node, positive below);
  // `labelDx` is new — at city scale, labels are placed *radially*
  // (pushed outward along the hub->node direction) rather than purely
  // vertically, so they extend into the empty space between sector
  // wedges instead of colliding with same-ring neighbours. Both are
  // assigned once by `computeCuratedLayout`'s greedy label placer, which
  // checks each node's candidate box against every already-placed label
  // to guarantee no two curated labels overlap (see that function's
  // docstring).
  labelDx?: number;
  labelDy?: number;
}

interface ClusterNodeDatum {
  id: string;
  layer: "observed";
  label: string;
  isOther: boolean;
  count: number;
  pulseSeverity: "normal" | "warning" | "critical";
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
}

type CityNodeDatum = CuratedNodeDatum | ClusterNodeDatum;

interface CuratedLinkDatum {
  id: string;
  source: string;
  target: string;
  layer: "curated";
  edgeType: string;
  isGatewayEdge: boolean;
  // Console redesign (D-R2): true when this link was produced by
  // collapsing >=1 real curated edge onto a sector aggregate node — never
  // an invented sector-pair connection (D11-1's honesty rule). `count` is
  // how many real edges it represents (always 1 when not aggregate).
  isAggregate: boolean;
  count: number;
}

interface ClusterLinkDatum {
  id: string;
  source: string;
  target: string;
  layer: "observed";
  count: number;
}

type CityLinkDatum = CuratedLinkDatum | ClusterLinkDatum;

// Bounded retry count for the post-mount rAF measurement loop below. A
// zero-sized first measurement can happen for entirely mundane reasons
// (layout not yet settled, a parent flex/grid pass not yet resolved,
// fonts still loading and nudging line-heights) — none of which fire a
// ResizeObserver callback if the box never actually *changes size* after
// that first paint (e.g. it was 0 and stays 0 for a tick, then the next
// rAF measurement already sees the real size, so there is no "resize"
// event to observe, only a late synchronous read to retry). Capped so a
// container that is legitimately, permanently zero-sized (e.g. rendered
// off-screen) doesn't retry forever.
const MEASURE_RETRY_LIMIT = 30;

function useContainerSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  // Synchronous first measurement, before paint: don't rely solely on the
  // async ResizeObserver callback for the *initial* size. Historically
  // this was ResizeObserver-only, and after the height-chain changes in
  // the previous fix round the observer's first callback sometimes never
  // delivered a non-zero contentRect on a clean mount, leaving `size` at
  // its zero initial value forever — the render guard below then never
  // flips true and the canvas silently never mounts (HIGH-1). Reading the
  // box directly here, plus a short bounded rAF retry loop for the case
  // where the very first layout pass still measures 0, closes that gap
  // without giving up the ResizeObserver (still needed for genuine later
  // resizes).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    let cancelled = false;
    let attempts = 0;
    let rafId: number | null = null;

    const measure = () => {
      if (cancelled) return;
      const rect = el.getBoundingClientRect();
      const width = Math.round(Math.max(0, rect.width));
      const height = Math.round(Math.max(0, rect.height));
      if (width > 0 && height > 0) {
        setSize((prev) => (prev.width === width && prev.height === height ? prev : { width, height }));
        return;
      }
      attempts += 1;
      if (attempts < MEASURE_RETRY_LIMIT) {
        rafId = requestAnimationFrame(measure);
      }
    };

    measure();
    return () => {
      cancelled = true;
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const width = Math.round(Math.max(0, entry.contentRect.width));
      const height = Math.round(Math.max(0, entry.contentRect.height));
      // Rounded, and only updated on a real change: `entry.contentRect` is
      // read from a container that in turn hosts the canvas this size
      // feeds (react-force-graph sets the canvas's pixel dimensions from
      // the `width`/`height` props below). Without `overflow-hidden` on
      // that container (see JSX) plus this guard, sub-pixel devicePixelRatio
      // rounding turns that into a feedback loop — each tick's canvas
      // resize nudges the container's measured content box up slightly,
      // which re-fires the observer, which grows the canvas again. Caught
      // in dev: 24 cluster nodes appearing pushed the canvas from ~503px
      // to >5500px tall within a few seconds before this guard was added.
      // Note this does NOT early-return on width===0/height===0: a real
      // resize down to 0 (e.g. a collapsed/hidden ancestor) must still be
      // reflected in state so the placeholder branch below can take over
      // instead of leaving a stale canvas size behind.
      setSize((prev) => (prev.width === width && prev.height === height ? prev : { width, height }));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return { ref, size };
}

/**
 * Deterministic-ish seed so re-mounts don't jump nodes around wildly;
 * layer bias keeps the two layers visually apart from the start. Used
 * for the cluster (observed) layer only — see `computeCuratedLayout` for
 * the curated layer, which is pinned rather than seeded. Ticket #16 FIX
 * round: the "right" side now seeds inside the narrow peripheral cluster
 * band (`CLUSTER_ZONE_LEFT_FRAC`, defined below) rather than the whole
 * right half of the canvas, so newly-arriving clusters don't have to
 * drift far to reach `makeClusterConfineForce`'s containment region.
 */
function seedPosition(width: number, height: number, side: "left" | "right") {
  const w = width || 600;
  const h = height || 400;
  const xBase = side === "left" ? w * 0.28 : w * (CLUSTER_ZONE_LEFT_FRAC + (1 - CLUSTER_ZONE_LEFT_FRAC) * 0.55);
  const spread = side === "left" ? w * 0.3 : w * (1 - CLUSTER_ZONE_LEFT_FRAC) * 0.7;
  return {
    x: xBase + (Math.random() - 0.5) * spread,
    y: h / 2 + (Math.random() - 0.5) * h * 0.82,
  };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/**
 * Base radius from criticality alone — Phase 2 widened this from `3 +
 * criticality*7` (3-10px, too small for a legible icon glyph) to
 * `11 + criticality*7` (11-18px). The dynamic range is deliberately
 * compressed: this was never meant to be a precise criticality readout —
 * severity/cascade rings already carry that signal — it just needs to
 * leave room for `asset-icons.ts` glyphs (Phase 3).
 */
function curatedNodeRadius(criticality: number): number {
  return 11 + criticality * 7;
}

/**
 * The ONE place hub/gateway/aggregate radius enlargement is computed —
 * consumed by both `computeCuratedLayout` (pre-measurement, reserves
 * label/spacing room) and `nodeCanvasObject` (paint) so the two can never
 * disagree about how big a marker actually is. Before Phase 2 the layout
 * used the raw criticality radius while paint independently enlarged the
 * hub/gateway, so the layout reserved space for a smaller circle than was
 * actually drawn — this also feeds `nodeVal` for hit-testing, so a click
 * target now always matches what's on screen.
 */
function curatedMarkerRadius(node: { id: string; criticality: number; isAggregate: boolean; isGateway: boolean }): number {
  const base = curatedNodeRadius(node.criticality);
  if (node.id === HUB_ASSET_NAME) return Math.max(base * 1.45, 24);
  if (node.isAggregate) return base * 1.2;
  if (node.isGateway) return Math.max(base, 13);
  return base;
}

// ---------------------------------------------------------------------------
// Console redesign (docs/PHASE5_CONSOLE_REDESIGN_PLAN.md §3, D-R2):
// sector-aggregated default view. `HUB_ASSET_NAME`/`SECTOR_ORDER`/
// `sectorLabel`/`sectorNodeId`/`groupNodesBySector` live in `@/lib/sectors`
// (shared with `SectorHealthStrip`, which needs the identical grouping so
// the strip's chips and the graph's aggregate nodes can never disagree
// about sector membership). `GET /api/topology` carries a real `sector`
// field per node (sourced from `config.SMART_CITY_ASSETS`, the one
// permitted backend touch this plan allows) — this replaces the Ticket #16
// frontend-only `SECTOR_OF` lookup table entirely; sector membership is
// derived from real data, never guessed here. A `null` sector (gateways,
// the synthesized `City_Grid` node) resolves to `CORE_SECTOR` (see that
// constant's docstring in `@/lib/sectors`) rather than being omitted: an
// earlier revision dropped these ~5 nodes from the aggregated (default)
// view entirely, along with every edge touching them, which hid the
// actual access-control layer a cascade routes through even after
// expanding every real sector by hand. They now get their own
// "Infrastructure" chip/bubble, exactly like Finance or Energy.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// D-R2: the actual node/edge set handed to the layout + rendering pipeline
// depends on view mode. `expanded` (the maximise control) always draws the
// real, unmodified topology — identical to the pre-redesign behavior.
// Otherwise the default sector-aggregated view draws the hub, one
// aggregate node per non-empty sector (sized by member count, D-R2), and —
// for every sector in `focusedSectors` — each one's real members in place
// of its single aggregate node ("clicking a sector expands that sector
// inline", D-R2). Stackable: any number of sectors may be focused at
// once, not just one.
// ---------------------------------------------------------------------------
interface DisplayNode {
  name: string;
  label: string;
  type: string | null;
  criticality: number;
  purdue_level: number | null;
  sector: string | null;
  is_gateway: boolean;
  isFinancial: boolean;
  isAggregate: boolean;
  memberCount: number;
}

interface DisplayEdge {
  source: string;
  target: string;
  edge_type: string;
  is_gateway_edge: boolean;
  /** True when this link was produced by collapsing >=1 real edge onto a sector aggregate node (either endpoint, or both). */
  isAggregate: boolean;
  /** How many real curated edges this one link represents — always 1 for a non-aggregate edge. */
  count: number;
}

function realNodeToDisplay(n: TopologyResponse["nodes"][number]): DisplayNode {
  return {
    name: n.name,
    label: n.name,
    type: n.type,
    criticality: n.criticality,
    purdue_level: n.purdue_level,
    sector: n.sector,
    is_gateway: n.is_gateway,
    isFinancial: (n.type ?? "").includes("Financial"),
    isAggregate: false,
    memberCount: 1,
  };
}

/**
 * Builds the node/edge set actually laid out and rendered for the current
 * view. Edge aggregation (the sector<->sector / hub<->sector links in the
 * default view) is derived strictly by remapping each REAL curated edge's
 * endpoints onto their display id and collapsing duplicates — never
 * inventing a sector-pair connection no real edge crosses (D11-1's
 * honesty rule, restated for this layer in the plan's §3). An edge is
 * dropped entirely if either endpoint has no display representation
 * (gateways/City_Grid in the aggregated view) or if remapping collapses it
 * to a self-loop (both ends land on the same node, e.g. two members of the
 * same still-aggregated sector).
 */
function buildDisplayTopology(
  topology: TopologyResponse,
  expanded: boolean,
  focusedSectors: ReadonlySet<string>,
  sectorMembers: Map<string, TopologyResponse["nodes"]>
): { nodes: DisplayNode[]; edges: DisplayEdge[] } {
  if (expanded) {
    return {
      nodes: topology.nodes.map(realNodeToDisplay),
      edges: topology.edges.map((e) => ({
        source: e.source,
        target: e.target,
        edge_type: e.edge_type,
        is_gateway_edge: e.is_gateway_edge,
        isAggregate: false,
        count: 1,
      })),
    };
  }

  const expandedNames = new Set<string>([HUB_ASSET_NAME]);
  for (const sector of focusedSectors) {
    for (const m of sectorMembers.get(sector) ?? []) expandedNames.add(m.name);
  }

  const nodes: DisplayNode[] = [];
  for (const n of topology.nodes) {
    if (expandedNames.has(n.name)) nodes.push(realNodeToDisplay(n));
  }
  const maxMembers = Math.max(1, ...[...sectorMembers.values()].map((v) => v.length));
  for (const sector of SECTOR_ORDER) {
    if (focusedSectors.has(sector)) continue;
    const members = sectorMembers.get(sector);
    if (!members || members.length === 0) continue;
    nodes.push({
      name: sectorNodeId(sector),
      label: `${sectorLabel(sector)} · ${members.length}`,
      type: null,
      // Sized by member count (D-R2), not criticality — reuses
      // `curatedNodeRadius`'s criticality->radius formula by mapping
      // count onto the same [0,1]-ish domain, so aggregate nodes need no
      // separate sizing code path in `nodeCanvasObject`.
      criticality: clamp(members.length / maxMembers, 0.2, 1),
      purdue_level: null,
      sector,
      is_gateway: false,
      isFinancial: false,
      isAggregate: true,
      memberCount: members.length,
    });
  }

  // Shared with `buildSectorByName`'s other caller (the main component's
  // own `sectorByName` below) rather than a second inline duplicate — both
  // now resolve gateways/City_Grid to CORE_SECTOR instead of `null`, which
  // is what lets an edge touching one of them get a real aggregate
  // endpoint (`sectorNodeId(CORE_SECTOR)`) instead of being dropped.
  const sectorByName = buildSectorByName(topology.nodes);
  const remap = (name: string): string | null => {
    if (expandedNames.has(name)) return name;
    const sector = sectorByName.get(name);
    return sector ? sectorNodeId(sector) : null;
  };

  const edgeMap = new Map<string, DisplayEdge>();
  for (const e of topology.edges) {
    const s = remap(e.source);
    const t = remap(e.target);
    if (!s || !t || s === t) continue;
    const isAgg = s !== e.source || t !== e.target;
    const key = `${s}->${t}`;
    const existing = edgeMap.get(key);
    if (existing) {
      existing.count += 1;
    } else {
      edgeMap.set(key, {
        source: s,
        target: t,
        edge_type: isAgg ? "aggregated" : e.edge_type,
        is_gateway_edge: isAgg ? false : e.is_gateway_edge,
        isAggregate: isAgg,
        count: 1,
      });
    }
  }

  return { nodes, edges: [...edgeMap.values()] };
}

/**
 * Labels that must stay permanently on regardless of collision pressure
 * (D-C5: "the hub, ALL financial assets, and any cascade-involved node
 * must stay permanently labelled"). Cascade involvement is dynamic (it
 * depends on the live `cascade` state), so it is *not* decided here —
 * `nodeCanvasObject` below checks it at paint time. This only covers the
 * two static cases plus a criticality floor so the low-value periphery
 * (sensors, advisory feeds) is the part that goes hover-only, not
 * anything actually consequential.
 */
const ALWAYS_LABEL_CRITICALITY = 0.5;

/**
 * Ticket #16 (D-C5), re-tuned in the FIX round: hub-centred
 * concentric/radial layout, replacing the Ticket #14 Purdue-columns
 * layout. At ~44 curated assets the column layout became a wall of text
 * and had no way to express "one node is the centre of the city" — this
 * does, literally: `City_Operations_Center` is pinned at the curated
 * region's centre, every other sector gets its own angular wedge around
 * it, and Purdue level becomes distance from centre *within* a wedge
 * (field/OT devices ring the outside, enterprise/external systems sit
 * closer in) — so the Purdue story that the column layout told
 * left-to-right is now told centre-to-edge. Gateways and the synthesized
 * `City_Grid` node aren't owned by one sector, so they ring the hub at a
 * small fixed "core" radius instead of a wedge (see `sectorOf`).
 *
 * FIX round change: the first pass used a single scalar radius bounded
 * by whichever of width/height was tighter, which on a wide 4:3-ish
 * container is *always* height — so the curated city never grew past
 * ~half the available width no matter how much horizontal room existed,
 * producing the "cramped left-centre 30%" defect. This version computes
 * independent `radiusX`/`radiusY` (an ellipse, not a circle): `radiusX`
 * is bounded only by the container's left edge and the cluster band's
 * left edge (`CLUSTER_ZONE_LEFT_FRAC`); `radiusY` only by top/bottom
 * margins. Because `radiusX` no longer has to fit inside `radiusY`'s
 * budget, sector wedges actually separate horizontally on a wide canvas
 * instead of collapsing into the old circle's tight radius. Every radius
 * used below (core ring, per-Purdue-level bands) is expressed as a
 * *fraction* of (radiusX, radiusY) and only converted to a pixel
 * position at the point a node's (x, y) is actually set — see
 * `ellipsePoint`.
 *
 * Positions are still handed back for `fx`/`fy` pinning (Ticket #14,
 * D14-1: a stationary curated layer is what keeps labels and the cascade
 * readable — more true, not less, at 44+ labels), and the label placer
 * below still checks each node's candidate label box against *every*
 * already-placed box (not just wedge neighbours) to guarantee no two
 * curated labels overlap — it now searches radially outward from each
 * node (see the placer's own comment) rather than only above/below,
 * since labels pushed straight up/down stop reading as "belonging to"
 * their wedge once wedges are angularly separated instead of stacked in
 * columns.
 */
function computeCuratedLayout(
  nodes: DisplayNode[],
  edges: DisplayEdge[],
  width: number,
  height: number
): { positions: Map<string, { x: number; y: number; labelDx: number; labelDy: number }>; labelMaxWidth: number } {
  const w = width || 600;
  const h = height || 400;
  void edges; // no longer used for layout (no barycenter sweep) — kept in the signature so callers don't change

  const cx = w * 0.42;
  const cy = h * 0.5;
  const clusterZoneLeft = w * CLUSTER_ZONE_LEFT_FRAC;
  const radiusX = Math.max(50, Math.min(cx - w * 0.03, clusterZoneLeft - w * CLUSTER_ZONE_GAP_FRAC - cx));
  const radiusY = Math.max(50, Math.min(cy - h * 0.06, h * 0.94 - cy));
  const ellipsePoint = (angle: number, frac: number) => ({
    x: cx + Math.cos(angle) * frac * radiusX,
    y: cy + Math.sin(angle) * frac * radiusY,
  });

  // Phase 2: raised alongside the larger hub halo (24px+ now, was 16px) —
  // at the old 0.15 the enlarged hub collides with the 5-node core ring.
  const coreFrac = 0.22;
  const minSectorFrac = coreFrac * 1.55;

  const hub = nodes.find((n) => n.name === HUB_ASSET_NAME);
  const core: DisplayNode[] = [];
  const bySector = new Map<string, DisplayNode[]>();
  for (const n of nodes) {
    if (n.name === HUB_ASSET_NAME) continue;
    // Console redesign: `n.sector` is now real data (either the backend's
    // `TopologyNode.sector` passthrough for a real asset, or the sector a
    // synthetic aggregate node itself represents — see
    // `buildDisplayTopology`), replacing the old name-lookup `sectorOf()`.
    const sector = n.sector ?? CORE_SECTOR;
    if (sector === CORE_SECTOR) {
      core.push(n);
      continue;
    }
    const list = bySector.get(sector);
    if (list) list.push(n);
    else bySector.set(sector, [n]);
  }

  const basePositions = new Map<string, { x: number; y: number }>();
  if (hub) basePositions.set(hub.name, { x: cx, y: cy });

  // Core ring (gateways, City_Grid): evenly spaced immediately around the
  // hub, sorted by name for a stable arrangement across re-renders.
  const sortedCore = [...core].sort((a, b) => a.name.localeCompare(b.name));
  sortedCore.forEach((n, i) => {
    const angle = -Math.PI / 2 + (i / Math.max(1, sortedCore.length)) * 2 * Math.PI;
    basePositions.set(n.name, ellipsePoint(angle, coreFrac));
  });

  // Radius fraction by Purdue level: level 0 (field/OT) sits at frac 1.0
  // (the outer rim), level 5 (external-facing) sits at `minSectorFrac` —
  // the D-C5 "Purdue story survives as radius instead of column"
  // requirement. A null level (shouldn't occur among sector members
  // today — City_Grid, the only null-level node, lives in the core ring
  // above) falls to a mid-band.
  const NULL_LEVEL = 2.5;
  const fracForLevel = (lvl: number | null) => {
    const clamped = lvl === null ? NULL_LEVEL : Math.max(0, Math.min(5, lvl));
    const frac = 1 - clamped / 5;
    return minSectorFrac + frac * (1 - minSectorFrac);
  };

  const activeSectors = SECTOR_ORDER.filter((s) => bySector.has(s));
  const wedgeAngle = activeSectors.length > 0 ? (2 * Math.PI) / activeSectors.length : 2 * Math.PI;

  activeSectors.forEach((sector, sIdx) => {
    const wedgeCenter = -Math.PI / 2 + sIdx * wedgeAngle;
    const members = [...(bySector.get(sector) ?? [])].sort((a, b) => {
      if (b.criticality !== a.criticality) return b.criticality - a.criticality;
      return a.name.localeCompare(b.name);
    });
    const byLevel = new Map<number, DisplayNode[]>();
    for (const m of members) {
      const lvl = m.purdue_level ?? NULL_LEVEL;
      const list = byLevel.get(lvl);
      if (list) list.push(m);
      else byLevel.set(lvl, [m]);
    }
    for (const [lvl, group] of byLevel) {
      const frac = fracForLevel(lvl);
      // Fan out within ~86% of the wedge so adjacent sectors never touch,
      // even when one radius band is crowded.
      const spread = wedgeAngle * 0.86;
      group.forEach((n, i) => {
        const spreadFrac = group.length > 1 ? i / (group.length - 1) - 0.5 : 0;
        const angle = wedgeCenter + spreadFrac * spread;
        basePositions.set(n.name, ellipsePoint(angle, frac));
      });
    }
  });

  // Defensive fallback: every display node is the hub, a core node, or a
  // sector member above, but a future asset added to config.py without a
  // `sector` value still falls into CORE_SECTOR via the `n.sector ?? CORE_SECTOR`
  // check above, so this should never actually trigger — kept so a
  // missing lookup degrades to "drawn at the hub" rather than crashing
  // the layout pass.
  for (const n of nodes) {
    if (!basePositions.has(n.name)) basePositions.set(n.name, { x: cx, y: cy });
  }

  // Per-node label budget in screen px, sized off the tighter of the two
  // radial spacings (a proxy for eventual on-screen px so `fitLabel`
  // truncates consistently) — raised from the first pass's 60-140 clamp
  // now that the ellipse gives real room to place a label in.
  const labelMaxWidth = clamp(Math.min(radiusX, radiusY) * 0.6, 80, 190);

  // Greedy sequential label placement (HIGH-1, re-tuned in the FIX
  // round): process nodes by distance from the hub (closest first, so
  // the core ring — which has the least room — claims its slots before
  // the open outer rim does), and for each pick the nearest still-free
  // slot along a *radially outward* search from the node's own position
  // (straight up for the hub itself, where no radial direction exists).
  // Candidates are tried nearest-ring-first, and within a ring across a
  // handful of small angular jitters off the pure radial line, so a
  // label collision with a same-wedge neighbour at an adjacent Purdue
  // level gets resolved by swinging sideways before falling back to a
  // farther ring. `CHAR_WIDTH_PX` is a monospace estimate at the ~10px
  // render font — it doesn't need to be exact, only consistent enough
  // that boxes computed here are a reasonable proxy for what `fitLabel`
  // + `ctx.measureText` actually draw later.
  const LABEL_HEIGHT_PX = 14;
  const CHAR_WIDTH_PX = 6;
  const MIN_GAP_PX = 5;
  const RING_COUNT = 6;
  const ANGLE_JITTERS = [0, 0.32, -0.32, 0.64, -0.64, 1.0, -1.0];
  interface Box {
    left: number;
    right: number;
    top: number;
    bottom: number;
  }
  const boxesOverlap = (a: Box, b: Box) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

  const order = [...nodes].sort((a, b) => {
    const pa = basePositions.get(a.name)!;
    const pb = basePositions.get(b.name)!;
    const da = (pa.x - cx) ** 2 + (pa.y - cy) ** 2;
    const db = (pb.x - cx) ** 2 + (pb.y - cy) ** 2;
    return da - db;
  });
  const placedBoxes: Box[] = [];
  const positions = new Map<string, { x: number; y: number; labelDx: number; labelDy: number }>();
  for (const n of order) {
    const pos = basePositions.get(n.name)!;
    const r = curatedMarkerRadius({ id: n.name, criticality: n.criticality, isAggregate: n.isAggregate, isGateway: n.is_gateway });
    // `n.label`, not `n.name`: for a sector aggregate node these differ
    // (id `sector:energy` vs. rendered label `"Energy · 5"`) and this
    // estimate must track whatever `fitLabel`/`ctx.measureText` actually
    // paint later.
    const halfWidth = Math.min(shortenLabel(n.label).length * CHAR_WIDTH_PX, labelMaxWidth) / 2 + 3;

    const distFromHub = Math.hypot(pos.x - cx, pos.y - cy);
    const baseAngle = distFromHub < 1 ? -Math.PI / 2 : Math.atan2(pos.y - cy, pos.x - cx);

    let chosenDx = Math.cos(baseAngle) * (r + MIN_GAP_PX);
    let chosenDy = Math.sin(baseAngle) * (r + MIN_GAP_PX);
    const boxFor = (dx: number, dy: number): Box => {
      const above = dy < 0;
      return {
        left: pos.x + dx - halfWidth,
        right: pos.x + dx + halfWidth,
        top: above ? pos.y + dy - LABEL_HEIGHT_PX : pos.y + dy,
        bottom: above ? pos.y + dy : pos.y + dy + LABEL_HEIGHT_PX,
      };
    };
    let chosenBox: Box = boxFor(chosenDx, chosenDy);
    let placed = false;
    for (let ring = 0; ring < RING_COUNT && !placed; ring++) {
      const dist = r + MIN_GAP_PX + ring * (LABEL_HEIGHT_PX + 4);
      for (const jitter of ANGLE_JITTERS) {
        const angle = baseAngle + jitter;
        const dx = Math.cos(angle) * dist;
        const dy = Math.sin(angle) * dist;
        const box = boxFor(dx, dy);
        if (!placedBoxes.some((b) => boxesOverlap(box, b))) {
          chosenDx = dx;
          chosenDy = dy;
          chosenBox = box;
          placed = true;
          break;
        }
      }
    }
    placedBoxes.push(chosenBox);
    positions.set(n.name, { x: pos.x, y: pos.y, labelDx: chosenDx, labelDy: chosenDy });
  }

  return { positions, labelMaxWidth };
}

// Ticket #16 FIX round (HIGH-1c): a small set of generic suffixes that
// carry no disambiguating information once a node is already drawn with
// its own marker shape/position (`_System`, `_Network`, `_Sensors`, plus
// a few equally generic siblings actually present in
// `config.SMART_CITY_ASSETS` — `_Facility`, `_Infrastructure`,
// `_Platform`, `_Feed`). Dropping one of these *before* `fitLabel` runs
// means the always-on label set (hub, financial, gateway, high-criticality,
// cascade-involved) reads as a shortened real word — `Metro_Signalling`,
// `Water_Quality` — instead of `fitLabel` falling back to mid-word
// character truncation (`Powe…`) to hit the same width budget. Order
// matters (checked longest-first) so `_Infrastructure` doesn't get
// shadowed by a shorter false match. Strips at most one trailing suffix;
// the full, unshortened name is still what's stored on the node and
// shown in the hover tooltip (`nodeLabel`) and cascade caption.
const DROPPABLE_SUFFIXES = ["_Infrastructure", "_System", "_Network", "_Sensors", "_Facility", "_Platform", "_Feed"];
function shortenLabel(name: string): string {
  for (const suffix of DROPPABLE_SUFFIXES) {
    if (name.length - suffix.length >= 4 && name.endsWith(suffix)) {
      return name.slice(0, -suffix.length);
    }
  }
  return name;
}

/**
 * Ticket #14 FIX round (HIGH-1): shrinks `text` to fit `maxWidth` (as
 * measured by the canvas context's *current* font), preferring to drop
 * trailing `_`-separated tokens first — e.g. `Municipal_Bond_Platform`
 * -> `Municipal_Bond…` before `Municipal…` — so truncation reads as
 * "the important prefix, abbreviated" rather than a word chopped in
 * half. This is a deterministic width-based rule, not a per-node lookup
 * table: it applies identically to every curated label and only kicks
 * in when the label actually doesn't fit at the current zoom. The full
 * name remains available via `nodeLabel` (hover tooltip). Callers pass
 * `shortenLabel(name)` first (see `nodeCanvasObject`) so this only has
 * to fall back to `…`-truncation on the already-shortened text.
 */
function fitLabel(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (ctx.measureText(text).width <= maxWidth) return text;
  const tokens = text.split("_");
  for (let keep = tokens.length - 1; keep >= 1; keep--) {
    const candidate = `${tokens.slice(0, keep).join("_")}…`;
    if (ctx.measureText(candidate).width <= maxWidth) return candidate;
  }
  let s = text;
  while (s.length > 1 && ctx.measureText(`${s}…`).width > maxWidth) {
    s = s.slice(0, -1);
  }
  return `${s}…`;
}

/**
 * Ticket #14 (D14-1): keeps the force-driven cluster layer confined to its
 * own region so it stays visually separable from the pinned curated
 * layer even as up to `CLUSTER_CAP` nodes arrive and their mutual charge
 * force pushes them around. Registered once via `fg.d3Force('clusterConfine', ...)`.
 *
 * This does NOT touch curated nodes at all — they are immune by
 * construction (`fx`/`fy` override any force's effect on position every
 * tick), so the guard here is purely about keeping clusters from
 * drifting out of their peripheral band, not about protecting the
 * curated nodes a second time.
 *
 * Ticket #16 FIX round: `minX` now matches `CLUSTER_ZONE_LEFT_FRAC` (the
 * curated layer's own boundary in `computeCuratedLayout`) instead of the
 * old 0.52w — the two constants defining "where the curated city ends
 * and the cluster band begins" must agree, or one layer would visually
 * bleed into the other's space.
 */
function makeClusterConfineForce(
  nodesMapRef: { current: Map<string, CityNodeDatum> },
  sizeRef: { current: { width: number; height: number } }
) {
  return function clusterConfineForce(alpha: number) {
    const { width, height } = sizeRef.current;
    if (width <= 0 || height <= 0) return;
    const targetX = width * (CLUSTER_ZONE_LEFT_FRAC + (1 - CLUSTER_ZONE_LEFT_FRAC) * 0.55);
    const minX = width * CLUSTER_ZONE_LEFT_FRAC;
    const topY = height * 0.04;
    const bottomY = height * 0.96;
    for (const node of nodesMapRef.current.values()) {
      if (node.layer !== "observed") continue;
      const x = node.x ?? targetX;
      const y = node.y ?? height / 2;
      node.vx = (node.vx ?? 0) + (targetX - x) * 0.01 * alpha;
      if (x < minX) node.vx = (node.vx ?? 0) + (minX - x) * 0.2;
      if (y < topY) node.vy = (node.vy ?? 0) + (topY - y) * 0.2;
      else if (y > bottomY) node.vy = (node.vy ?? 0) + (bottomY - y) * 0.2;
    }
  };
}

interface CascadeState {
  originAsset: string;
  ciiMedian: number;
  ciiP5: number;
  ciiP95: number;
  impacted: string[];
  impactedSet: Set<string>;
  /** Asset name -> hop distance from origin along curated edges (undirected BFS — see `computeCascadeGeometry`). */
  hopOf: Map<string, number>;
  /** Curated link ids (`curated:src->tgt`) on a shortest path from origin to some impacted asset. */
  pathEdgeIds: Set<string>;
  /** Curated link id -> hop of the far (child) endpoint, i.e. when that edge should light up. */
  edgeHopOf: Map<string, number>;
  /** Hop assigned to impacted assets the BFS never reached (disconnected from origin in the curated graph) — still revealed, just last and without a lit path. */
  fallbackHop: number;
  startedAt: number;
}

/**
 * Ticket #14 (D14-2): derives *where* to draw the real `impacted` list,
 * strictly from the real curated topology edges — never a hardcoded or
 * scripted path. BFS is run undirected (curated edges have a
 * `src`/`tgt` direction for dependency semantics, not necessarily the
 * direction compromise propagates) purely to find a plausible on-graph
 * route and hop count for staggering the reveal; it invents no edges and
 * changes no node's impacted/not-impacted status — that verdict comes
 * entirely from `cii.impacted`.
 */
function computeCascadeGeometry(
  origin: string,
  impacted: string[],
  edges: TopologyResponse["edges"]
): { hopOf: Map<string, number>; pathEdgeIds: Set<string>; edgeHopOf: Map<string, number>; fallbackHop: number } {
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
  // Only keep edge ids that actually exist as curated links (the two
  // directions added above are a lookup convenience, not a claim both
  // exist — `linksMapRef` only ever holds the real one per D11-1).
  let maxKnownHop = 0;
  for (const h of hopOf.values()) maxKnownHop = Math.max(maxKnownHop, h);

  return { hopOf, pathEdgeIds, edgeHopOf, fallbackHop: maxKnownHop + 1 };
}

export function CityGraph({ topology }: { topology: TopologyResponse }) {
  // D14-2 constraint: consume the shared `useStream()` context only —
  // never call `useEventStream()` directly (that reintroduces the
  // duplicate-socket defect Ticket #10 fixed).
  const { status, events, latestCii } = useStream();
  const colors = useThemeColors();
  const monoFont = useMonoFontFamily();
  const reducedMotion = usePrefersReducedMotion();
  const { ref: containerRef, size } = useContainerSize<HTMLDivElement>();
  // Console redesign (D-R2): shared with `GraphPanel` (the maximise
  // control) and `SectorHealthStrip` (sector chips) via context — see
  // `@/lib/graph-focus-context` for why this can't be local state.
  const { expanded, setExpanded, focusedSectors, toggleFocusedSector, focusSector, clearFocusedSectors } =
    useGraphFocus();

  const knownAssets = useMemo(
    () => new Set(topology.nodes.map((n) => n.name)),
    [topology]
  );

  // Real (non-hub) curated nodes grouped by sector — the input to both the
  // default aggregated view and the per-sector "worst severity"/"live
  // count" rollups below. `sectorByName` is the inverse lookup (asset name
  // -> sector), used to auto-focus a cascade's own sector (see the
  // `latestCii` effect further down) and by `SectorHealthStrip` indirectly
  // through the same `topology` it reads from `useTopology()`.
  const sectorMembers = useMemo(() => groupNodesBySector(topology.nodes), [topology]);
  const sectorByName = useMemo(() => buildSectorByName(topology.nodes), [topology]);

  // The node/edge set this render actually lays out and draws — see
  // `buildDisplayTopology`'s docstring for expanded vs. sector-aggregated
  // vs. one-sector-focused semantics.
  const displayTopology = useMemo(
    () => buildDisplayTopology(topology, expanded, focusedSectors, sectorMembers),
    [topology, expanded, focusedSectors, sectorMembers]
  );

  // Escape collapses the maximised view and clears every focused sector —
  // "Escape or the same control returns" (D-R2). Only attached while there
  // is something to collapse, so it never intercepts Escape elsewhere on
  // the page (a stray listener firing on every keypress regardless of
  // state would be its own kind of bug).
  useEffect(() => {
    if (!expanded && focusedSectors.size === 0) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      setExpanded(false);
      clearFocusedSectors();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expanded, focusedSectors, setExpanded, clearFocusedSectors]);

  // Persistent, mutated-in-place stores — see module docstring on why
  // object identity must survive across ticks.
  const nodesMapRef = useRef(new Map<string, CityNodeDatum>());
  const linksMapRef = useRef(new Map<string, CityLinkDatum>());
  const clusterAggregatorRef = useRef(new ClusterAggregator());
  const assetActivityRef = useRef(new AssetActivityTracker());
  const fgRef = useRef<ForceGraphMethods<CityNodeDatum, CityLinkDatum> | undefined>(undefined);
  // Distinguishes our own `zoomToFit` calls from a real user pan/zoom,
  // both of which fire `onZoomEnd` — see the reframe effects below. A
  // count, not a boolean: Ticket #14 added a second reframe trigger (the
  // curated-layout effect, on top of the pre-existing node-count-growth
  // trigger), so two `reframe()` calls can now legitimately overlap —
  // e.g. one fired at t=60ms with a 300ms animation and another at
  // t=80ms with a 400ms animation. With a boolean, the FIRST call's
  // reset timer (at t=410ms) clears the flag before the SECOND call's
  // animation actually finishes (at t=480ms); that second animation's
  // own `onZoomEnd` then fires with the flag already false and gets
  // misread as a real user pan, setting `userFramedRef` and silently
  // disabling all future auto-reframing for the rest of the session —
  // reproduced while building this ticket (the curated layout computed
  // correctly, per `computeCuratedLayout`'s own positions, but the
  // camera stayed stuck zoomed into a stale sub-region). A count only
  // reaches zero once every outstanding programmatic zoom's own timer
  // has fired, so overlapping calls can no longer race each other.
  const programmaticZoomCountRef = useRef(0);
  const userFramedRef = useRef(false);
  const reframe = useCallback((duration: number) => {
    programmaticZoomCountRef.current += 1;
    fgRef.current?.zoomToFit(duration, 40);
    window.setTimeout(() => {
      programmaticZoomCountRef.current = Math.max(0, programmaticZoomCountRef.current - 1);
    }, duration + 50);
  }, []);
  const eventsRef = useRef(events);
  useEffect(() => {
    eventsRef.current = events;
  }, [events]);

  const [graphData, setGraphData] = useState<{ nodes: CityNodeDatum[]; links: CityLinkDatum[] }>(
    { nodes: [], links: [] }
  );
  const [caption, setCaption] = useState(
    "Waiting for stream — no capture traffic observed yet."
  );
  const sizeRef = useRef(size);
  useEffect(() => {
    sizeRef.current = size;
  }, [size]);
  // Console redesign: previous-size bookkeeping for the reheat-on-resize
  // fix in the curated-layout effect below — see that effect's comment.
  const prevSizeRef = useRef<{ width: number; height: number } | null>(null);
  // Ticket #14 FIX round (HIGH-1): per-node label width budget (screen
  // px), recomputed alongside the curated layout below. Read every paint
  // frame by `nodeCanvasObject` via `fitLabel` — a ref, not state, since
  // it changes only when the layout effect runs, not on every render.
  const curatedLabelMaxWidthRef = useRef(120);

  // Re-frame the camera (a) immediately the first time the node count
  // grows past its prior high-water mark — most importantly when
  // observed clusters first appear: they seed on the right half per
  // `seedPosition`, and up to CLUSTER_CAP arriving in one batch under
  // d3-force repulsion can push the whole layout outside the initial
  // curated-only viewport — and (b) on a slow recurring cadence
  // thereafter, because D11-2's rank-based top-N churn keeps swapping
  // *which* `/24`s are rendered even once the node *count* has settled
  // at the cap, and each swapped-in node starts from a fresh seed
  // position that a count-only trigger would never notice needs
  // re-framing. This is camera framing, not simulation state — it does
  // not reseed or reheat the d3-force layout (see D11-2's "never
  // re-seed" guidance, which is about physics, not the viewport).
  const maxFramedNodeCountRef = useRef(0);
  useEffect(() => {
    const count = graphData.nodes.length;
    if (count === 0 || count <= maxFramedNodeCountRef.current) return;
    maxFramedNodeCountRef.current = count;
    const duration = reducedMotion ? 0 : 400;
    const timer = setTimeout(() => reframe(duration), 80);
    return () => clearTimeout(timer);
  }, [graphData.nodes.length, reducedMotion, reframe]);

  useEffect(() => {
    const duration = reducedMotion ? 0 : 600;
    const interval = setInterval(() => {
      // Skip once the operator has manually panned/zoomed — see
      // `onZoomEnd` below. Auto-reframing over top of a deliberate
      // manual view would be actively unhelpful, not just cosmetic.
      if (nodesMapRef.current.size > 0 && !userFramedRef.current) {
        reframe(duration);
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [reducedMotion, reframe]);

  // (Re)lay out and pin the curated layer whenever topology or the
  // measured container size changes (mount, a manual retry after a
  // topology fetch failure, or the initial 0->real size measurement).
  // Ticket #14 (D14-1): unlike the cluster tick below, this deliberately
  // recomputes and overwrites x/y/fx/fy every time it runs, not just on
  // create — the curated layer's whole point is that its nodes sit at
  // a stable, known position rather than wherever physics leaves them.
  // Re-running on a real size change (which happens once or twice right
  // after mount, then never again barring a window resize) rescales the
  // deterministic grid; it never happens on the ~100ms cluster tick, so
  // it does not fight that effect's "don't reheat every tick" discipline.
  useEffect(() => {
    const nodesMap = nodesMapRef.current;
    const linksMap = linksMapRef.current;
    const { nodes: displayNodes, edges: displayEdges } = displayTopology;
    const { positions, labelMaxWidth } = computeCuratedLayout(
      displayNodes,
      displayEdges,
      size.width,
      size.height
    );
    curatedLabelMaxWidthRef.current = labelMaxWidth;

    // Console redesign: the maximise control (D-R2) can change the
    // container's measured size drastically (e.g. ~738px sector view <->
    // ~1400px expanded), and `makeClusterConfineForce`'s target band
    // shifts with it every tick via `sizeRef`. Reheating alone
    // (`d3ReheatSimulation()`, resetting alpha to 1) turned out not to be
    // enough: the confine force's per-tick pull toward the band is
    // deliberately gentle (`(targetX - x) * 0.01 * alpha`, tuned for
    // nudging nodes that are already close), and `cooldownTicks={200}`
    // caps how many ticks the simulation ever runs — not nearly enough
    // budget for that gentle a pull to drag an already-settled cluster
    // node across a large distance (measured: reheating alone left the
    // whole `/24` layer stuck roughly where it was, visibly outside the
    // new band, after a single maximise -> collapse cycle). Directly
    // reseeding each observed node's (x, y) via the same `seedPosition()`
    // new cluster nodes already use — then reheating so the confine force
    // finishes the precise settling — fixes it unconditionally, the same
    // way a brand-new cluster node already avoids this problem by never
    // having a stale position to begin with. Curated nodes are untouched
    // either way (`fx`/`fy` pins override every force regardless).
    // Guarded to real, non-trivial size changes only — not on every
    // `displayTopology` change (sector focus/unfocus doesn't resize the
    // canvas and shouldn't needlessly disturb cluster physics).
    const prevSize = prevSizeRef.current;
    const sizeChanged =
      prevSize !== null &&
      (Math.abs(prevSize.width - size.width) > 4 || Math.abs(prevSize.height - size.height) > 4);
    prevSizeRef.current = { width: size.width, height: size.height };
    if (sizeChanged && size.width > 0 && size.height > 0) {
      for (const node of nodesMap.values()) {
        if (node.layer !== "observed") continue;
        const pos = seedPosition(size.width, size.height, "right");
        node.x = pos.x;
        node.y = pos.y;
      }
      fgRef.current?.d3ReheatSimulation();
    }

    // Console redesign: unlike the original (always-50-assets) topology,
    // the display node/edge SET now changes with view mode (expand/
    // collapse, focus/unfocus a sector) — a real asset or a sector
    // aggregate can disappear from one render to the next. Reconcile by
    // removing any curated node/link no longer present, mirroring the
    // add/remove pattern the ~100ms cluster tick below already uses for
    // the observed `/24` layer.
    const desiredNodeIds = new Set(displayNodes.map((n) => n.name));
    for (const [id, node] of nodesMap) {
      if (node.layer === "curated" && !desiredNodeIds.has(id)) nodesMap.delete(id);
    }
    const desiredLinkIds = new Set(displayEdges.map((e) => `curated:${e.source}->${e.target}`));
    for (const [id, link] of linksMap) {
      if (link.layer === "curated" && !desiredLinkIds.has(id)) linksMap.delete(id);
    }

    for (const n of displayNodes) {
      const pos = positions.get(n.name) ?? {
        x: (size.width || 600) / 2,
        y: (size.height || 400) / 2,
        labelDx: 0,
        labelDy: 12,
      };
      const existing = nodesMap.get(n.name) as CuratedNodeDatum | undefined;
      if (existing && existing.layer === "curated") {
        existing.label = n.label;
        existing.nodeType = n.type;
        existing.criticality = n.criticality;
        existing.isGateway = n.is_gateway;
        existing.isFinancial = n.isFinancial;
        existing.isAggregate = n.isAggregate;
        existing.memberCount = n.memberCount;
        existing.x = pos.x;
        existing.y = pos.y;
        existing.fx = pos.x;
        existing.fy = pos.y;
        existing.labelDx = pos.labelDx;
        existing.labelDy = pos.labelDy;
      } else {
        nodesMap.set(n.name, {
          id: n.name,
          layer: "curated",
          label: n.label,
          nodeType: n.type,
          criticality: n.criticality,
          isGateway: n.is_gateway,
          isFinancial: n.isFinancial,
          isAggregate: n.isAggregate,
          memberCount: n.memberCount,
          pulseSeverity: "normal",
          x: pos.x,
          y: pos.y,
          fx: pos.x,
          fy: pos.y,
          labelDx: pos.labelDx,
          labelDy: pos.labelDy,
        });
      }
    }
    for (const e of displayEdges) {
      const id = `curated:${e.source}->${e.target}`;
      const existing = linksMap.get(id) as CuratedLinkDatum | undefined;
      if (existing && existing.layer === "curated") {
        existing.isAggregate = e.isAggregate;
        existing.count = e.count;
      } else {
        linksMap.set(id, {
          id,
          source: e.source,
          target: e.target,
          layer: "curated",
          edgeType: e.edge_type,
          isGatewayEdge: e.is_gateway_edge,
          isAggregate: e.isAggregate,
          count: e.count,
        });
      }
    }
    setGraphData({ nodes: [...nodesMap.values()], links: [...linksMap.values()] });

    // The layout effect above can (and on mount, always does) run once
    // with 0x0 fallback dimensions before `useContainerSize` lands a real
    // measurement, then again with the real size — repositioning every
    // curated node. The node *count* doesn't change between those runs,
    // so the node-count-triggered reframe effect below never fires again
    // for it, and the camera is left fit to the stale fallback-size
    // layout (curated nodes bunched near the fallback 600x400 center)
    // instead of the real one. Re-frame here too, once real dimensions
    // are known, unless the operator has already manually framed.
    if (size.width > 0 && size.height > 0 && !userFramedRef.current) {
      const duration = reducedMotion ? 0 : 300;
      const timer = setTimeout(() => reframe(duration), 60);
      return () => clearTimeout(timer);
    }
  }, [displayTopology, size.width, size.height, reducedMotion, reframe]);

  // Ticket #14 (D14-1): register the cluster-confinement force once a
  // real size is known (and the `ForceGraph2D` below has actually
  // mounted, so `fgRef.current` exists). Re-registering under the same
  // force name on a later run (e.g. a resize) just replaces it — d3-force
  // does not accumulate duplicates.
  const hasRealSize = size.width > 0 && size.height > 0;
  useEffect(() => {
    if (!hasRealSize) return;
    const fg = fgRef.current;
    if (!fg) return;
    fg.d3Force("clusterConfine", makeClusterConfineForce(nodesMapRef, sizeRef));
  }, [hasRealSize]);

  // The throttled cluster/severity tick (D11-2). Mirrors TelemetryRail's
  // D10-3 pattern: a fixed interval reads the latest stream buffer via a
  // ref (cheap, no re-render trigger of its own) rather than reacting to
  // every WS message.
  useEffect(() => {
    const interval = setInterval(() => {
      const currentEvents = eventsRef.current;
      const aggregator = clusterAggregatorRef.current;
      const activity = assetActivityRef.current;
      aggregator.ingest(currentEvents);
      activity.ingest(currentEvents, knownAssets);

      const snapshot = aggregator.snapshot(CLUSTER_CAP, PULSE_WINDOW_MS);
      const nodesMap = nodesMapRef.current;
      const linksMap = linksMapRef.current;
      // Tracks whether the node/link *set* actually changed (an id was
      // added or removed) this tick, as opposed to just a mutable field
      // (count, label, pulseSeverity) changing on an existing object.
      // This matters because `setGraphData` re-registers the node/link
      // arrays with react-force-graph's underlying d3-force simulation
      // on every call — pure field mutations on existing, still-present
      // objects are already visible next animation frame without it
      // (the canvas paint reads straight from the live object
      // references). Calling `setGraphData` unconditionally every
      // ~100ms tick, even when nothing structural changed, was
      // continuously re-heating/reinitializing the simulation; under
      // sustained high-rate replay (speed=500) that repeated reheat
      // eventually corrupted node positions to NaN and the canvas went
      // silently blank (no error — `ctx.arc(NaN, NaN, ...)` just draws
      // nothing). Only calling it on genuine add/remove keeps physics
      // stable indefinitely.
      let structureChanged = false;

      // Reconcile cluster nodes: update-in-place or create; drop ones
      // that fell out of the top-N (or `other` disappearing entirely).
      const desiredClusterIds = new Set(snapshot.nodes.map((n: ClusterSnapshotNode) => n.id));
      for (const [id, node] of nodesMap) {
        if (node.layer === "observed" && !desiredClusterIds.has(id)) {
          nodesMap.delete(id);
          structureChanged = true;
        }
      }
      for (const s of snapshot.nodes) {
        const existing = nodesMap.get(s.id) as ClusterNodeDatum | undefined;
        if (existing && existing.layer === "observed") {
          existing.count = s.count;
          existing.isOther = s.isOther;
          existing.label = formatClusterLabel(s);
          existing.pulseSeverity = s.pulseSeverity;
        } else {
          const pos = seedPosition(sizeRef.current.width, sizeRef.current.height, "right");
          nodesMap.set(s.id, {
            id: s.id,
            layer: "observed",
            label: formatClusterLabel(s),
            isOther: s.isOther,
            count: s.count,
            pulseSeverity: s.pulseSeverity,
            ...pos,
          });
          structureChanged = true;
        }
      }

      // Reconcile observed-flow links the same way.
      const desiredLinkIds = new Set(
        snapshot.links.map((l) => `observed:${l.source}->${l.target}`)
      );
      for (const [id, link] of linksMap) {
        if (link.layer === "observed" && !desiredLinkIds.has(id)) {
          linksMap.delete(id);
          structureChanged = true;
        }
      }
      for (const l of snapshot.links) {
        const id = `observed:${l.source}->${l.target}`;
        const existing = linksMap.get(id) as ClusterLinkDatum | undefined;
        if (existing) {
          existing.count = l.count;
        } else {
          linksMap.set(id, { id, source: l.source, target: l.target, layer: "observed", count: l.count });
          structureChanged = true;
        }
      }

      // Curated-node pulse severity — a field mutation, not a structural
      // change; the canvas picks it up next frame regardless of whether
      // `setGraphData` runs below. A sector aggregate node (D-R2 "badged
      // by worst current severity") has no real events of its own — its
      // severity is the worst pulse among the REAL members it currently
      // stands in for, read from the same `AssetActivityTracker` that
      // already tracks every real curated asset regardless of whether
      // that asset is individually displayed right now.
      const SEVERITY_RANK = { normal: 0, warning: 1, critical: 2 } as const;
      for (const node of nodesMap.values()) {
        if (node.layer !== "curated") continue;
        if (node.isAggregate) {
          const sector = node.id.startsWith("sector:") ? node.id.slice("sector:".length) : null;
          const members = sector ? sectorMembers.get(sector) : undefined;
          let worst: "normal" | "warning" | "critical" = "normal";
          for (const m of members ?? []) {
            const s = activity.severityOf(m.name, PULSE_WINDOW_MS);
            if (SEVERITY_RANK[s] > SEVERITY_RANK[worst]) worst = s;
          }
          node.pulseSeverity = worst;
        } else {
          node.pulseSeverity = activity.severityOf(node.id, PULSE_WINDOW_MS);
        }
      }

      if (structureChanged) {
        setGraphData({ nodes: [...nodesMap.values()], links: [...linksMap.values()] });
      }

      const touched = activity.touchedAssetNames();
      if (touched.length > 0) {
        setCaption(
          `Observed traffic has touched ${touched.length} curated asset${touched.length === 1 ? "" : "s"} by name this session — the two layers are drawn separately regardless.`
        );
      } else if (snapshot.totalEvents > 0) {
        setCaption(
          `Observed capture traffic (${snapshot.totalSubnets.toLocaleString("en-US")} distinct /24 subnet${snapshot.totalSubnets === 1 ? "" : "s"}) does not intersect the modelled city assets — no edges are invented between the two layers.`
        );
      } else {
        setCaption("Waiting for stream — no capture traffic observed yet.");
      }
    }, RENDER_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [knownAssets, sectorMembers]);

  // Ticket #14 (D14-1): curated labels always show; cluster labels only
  // on hover, to keep up to 24 arriving cluster nodes from illegibly
  // overlapping each other and the curated layer's own labels. A ref,
  // not state — `nodeCanvasObject` reads it every paint frame and a
  // hover change shouldn't force a React re-render of this component.
  const hoveredNodeIdRef = useRef<string | null>(null);

  // Ticket #14 (D14-2): the cascade animation state, driven strictly by
  // the real `cii` envelope carried on the shared stream — never a
  // scripted/hardcoded path. `latestCii` is replaced wholesale by
  // `useEventStream` on every new envelope (no queue there), and this
  // effect mirrors that: a second envelope arriving mid-animation simply
  // replaces `cascade` outright, so there is never more than one
  // animation in flight and nothing to interrupt or clean up.
  const [cascade, setCascade] = useState<CascadeState | null>(null);
  const lastCiiRef = useRef<typeof latestCii>(null);
  useEffect(() => {
    if (!latestCii || latestCii === lastCiiRef.current) return;
    lastCiiRef.current = latestCii;

    const impacted = Array.isArray(latestCii.impacted)
      ? latestCii.impacted.filter((v): v is string => typeof v === "string")
      : [];
    const { hopOf, pathEdgeIds, edgeHopOf, fallbackHop } = computeCascadeGeometry(
      latestCii.origin_asset,
      impacted,
      topology.edges
    );
    const startedAt = performance.now();
    setCascade({
      originAsset: latestCii.origin_asset,
      ciiMedian: latestCii.cii_median,
      ciiP5: latestCii.cii_p5,
      ciiP95: latestCii.cii_p95,
      impacted,
      impactedSet: new Set(impacted),
      hopOf,
      pathEdgeIds,
      edgeHopOf,
      fallbackHop,
      startedAt,
    });

    // Auto-clear after the last hop has had time to reveal plus a hold —
    // `startedAt` is the identity guard: if a newer envelope replaced
    // `cascade` before this fires, `setCascade` here is a same-value
    // no-op against a functional update keyed on the timer's own
    // `startedAt`, so a stale timer can never clear a newer cascade.
    const clearAfterMs = fallbackHop * CASCADE_STAGGER_MS + CASCADE_HOLD_MS;
    const timer = window.setTimeout(() => {
      setCascade((current) => (current?.startedAt === startedAt ? null : current));
    }, clearAfterMs);

    // D-R2/D-R3 "cascade animation still animates on the real impacted
    // payload" must hold in the default sector-aggregated view too, not
    // only when already expanded — otherwise the origin asset itself can
    // be invisible (collapsed into its sector's single aggregate node)
    // the moment an attack fires. Auto-focus the origin's own sector so
    // the real origin node comes into view; other sectors still show
    // impacted membership via their aggregate node's severity/ring (see
    // the pulse-severity and cascade-ring logic elsewhere in this file).
    // Uses `focusSector` (ensure-focused), not the manual click path's
    // `toggleFocusedSector` — this must never UNfocus a sector an operator
    // already had open by hand, it only ever adds the origin's sector
    // alongside whatever's already focused (focus is stackable). No-op if
    // already expanded (everything is already visible). A gateway/City_Grid
    // origin now correctly auto-focuses `CORE_SECTOR` too, since
    // `sectorByName` (buildSectorByName) resolves them there instead of to
    // `null` — a honeytoken breach on a gateway used to leave the operator
    // staring at a compact view with no visible reason anything happened;
    // it now opens the Infrastructure bubble the same way a curated-asset
    // origin opens its own sector.
    if (!expanded) {
      const originSector = sectorByName.get(latestCii.origin_asset);
      if (originSector) focusSector(originSector);
    }

    return () => window.clearTimeout(timer);
  }, [latestCii, topology.edges, expanded, sectorByName, focusSector]);

  const nodeCanvasObject = useCallback(
    (node: CityNodeDatum, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      // Defensive: a d3-force position can in principle go NaN (e.g. an
      // extreme, momentary force-layout instability). Skipping the draw
      // for just that node/frame is preferable to `ctx.arc(NaN, ...)`,
      // which throws inside the canvas paint loop and — since
      // `autoPauseRedraw={false}` keeps requestAnimationFrame calling
      // this every frame — would otherwise repeat every frame.
      if (!Number.isFinite(x) || !Number.isFinite(y)) {
        return;
      }
      const t = reducedMotion ? 0 : performance.now();
      const pulseT = (Math.sin(t / 420) + 1) / 2; // 0..1

      const pulseColor =
        node.pulseSeverity === "critical"
          ? colors.sevCritical
          : node.pulseSeverity === "warning"
            ? colors.sevWarning
            : null;

      if (node.layer === "curated") {
        // Ticket #16 FIX round (HIGH-1d): the hub must be unmistakably
        // the visual centre, not just another dot with a bigger radius —
        // a distinct double-ring "sun" marker (soft outer halo, solid
        // filled ring, dark core) drawn before the gateway/financial
        // branches so it takes priority regardless of those flags.
        // `markerR` (Phase 2: from the shared `curatedMarkerRadius`,
        // which `computeCuratedLayout` also calls — layout and paint can
        // no longer disagree about how big a marker is) is what the
        // pulse/cascade rings below and the financial/default branches
        // are all sized off of; for a plain node it's just the raw
        // criticality radius, identical to the old `baseR`.
        const isHub = node.id === HUB_ASSET_NAME;
        const markerR = curatedMarkerRadius({
          id: node.id,
          criticality: node.criticality,
          isAggregate: node.isAggregate,
          isGateway: node.isGateway,
        });
        if (isHub) {
          const haloR = markerR + 8 + (reducedMotion ? 3 : pulseT * 5);
          ctx.beginPath();
          ctx.arc(x, y, haloR, 0, 2 * Math.PI);
          ctx.lineWidth = 1.5;
          ctx.strokeStyle = colors.graphAccentHi;
          ctx.globalAlpha = 0.4;
          ctx.stroke();
          ctx.globalAlpha = 1;

          ctx.beginPath();
          ctx.arc(x, y, markerR, 0, 2 * Math.PI);
          ctx.fillStyle = colors.graphAccentHi;
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = colors.text;
          ctx.stroke();

          ctx.beginPath();
          ctx.arc(x, y, markerR * 0.42, 0, 2 * Math.PI);
          ctx.fillStyle = colors.ground;
          ctx.fill();
        } else if (node.isGateway) {
          const r = markerR;
          ctx.beginPath();
          ctx.arc(x, y, r, 0, 2 * Math.PI);
          ctx.lineWidth = 1.6;
          ctx.strokeStyle = colors.graphAccentHi;
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(x, y, r * 0.55, 0, 2 * Math.PI);
          ctx.fillStyle = colors.graphAccent;
          ctx.globalAlpha = 0.55;
          ctx.fill();
          ctx.globalAlpha = 1;
        } else if (node.isFinancial) {
          const r = markerR;
          ctx.beginPath();
          ctx.moveTo(x, y - r);
          ctx.lineTo(x + r, y);
          ctx.lineTo(x, y + r);
          ctx.lineTo(x - r, y);
          ctx.closePath();
          ctx.fillStyle = colors.financial;
          ctx.fill();
        } else if (node.isAggregate) {
          // Console redesign (D-R2): a sector aggregate node — visually
          // distinct from a single real asset (a hairline outer ring, like
          // the gateway marker but filled solid) so an operator can tell
          // at a glance "this is a rolled-up sector" without reading the
          // label first.
          ctx.beginPath();
          ctx.arc(x, y, markerR, 0, 2 * Math.PI);
          ctx.fillStyle = colors.graphAccent;
          ctx.fill();
          ctx.beginPath();
          ctx.arc(x, y, markerR + 3, 0, 2 * Math.PI);
          ctx.lineWidth = 1.4;
          ctx.strokeStyle = colors.graphAccentHi;
          ctx.globalAlpha = 0.6;
          ctx.stroke();
          ctx.globalAlpha = 1;
        } else {
          ctx.beginPath();
          ctx.arc(x, y, markerR, 0, 2 * Math.PI);
          ctx.fillStyle = colors.graphAccent;
          ctx.fill();
        }

        if (pulseColor) {
          const ringR = markerR + 4 + (reducedMotion ? 3 : pulseT * 6);
          ctx.beginPath();
          ctx.arc(x, y, ringR, 0, 2 * Math.PI);
          ctx.lineWidth = 2;
          ctx.strokeStyle = pulseColor;
          ctx.globalAlpha = reducedMotion ? 0.9 : 0.55 + (1 - pulseT) * 0.35;
          ctx.stroke();
          ctx.globalAlpha = 1;
        }

        // Ticket #14 (D14-2): cascade overlay, strictly from the real
        // `cii` envelope (`cascade`) — never a scripted path. The origin
        // pulses critical; impacted assets light up in the order/hop
        // computed from the real curated edges + the real `impacted`
        // list (`computeCascadeGeometry`), staggered by
        // `CASCADE_STAGGER_MS` so the reveal reads as propagation rather
        // than everything flashing at once. `prefers-reduced-motion`
        // shows the fully-revealed end state with no motion.
        if (cascade) {
          if (node.id === cascade.originAsset) {
            const ringR = markerR + 7 + (reducedMotion ? 4 : pulseT * 8);
            ctx.beginPath();
            ctx.arc(x, y, ringR, 0, 2 * Math.PI);
            ctx.lineWidth = 2.4;
            ctx.strokeStyle = colors.sevCritical;
            ctx.globalAlpha = reducedMotion ? 0.95 : 0.6 + (1 - pulseT) * 0.35;
            ctx.stroke();
            ctx.globalAlpha = 1;
          } else if (
            node.isAggregate
              ? // A still-aggregated sector can hold a real impacted asset
                // the default view isn't showing individually right now —
                // the aggregate node itself lights up so the cascade stays
                // visible without forcing every sector open (D-R2/D-R3).
                (sectorMembers.get(node.id.slice("sector:".length)) ?? []).some((m) =>
                  cascade.impactedSet.has(m.name)
                )
              : cascade.impactedSet.has(node.id)
          ) {
            const hop = cascade.hopOf.get(node.id) ?? cascade.fallbackHop;
            const elapsed = reducedMotion ? Infinity : performance.now() - cascade.startedAt;
            if (elapsed >= hop * CASCADE_STAGGER_MS) {
              const ringR = markerR + 5;
              ctx.beginPath();
              ctx.arc(x, y, ringR, 0, 2 * Math.PI);
              ctx.lineWidth = 2;
              ctx.strokeStyle = colors.sevWarning;
              ctx.globalAlpha = 1;
              ctx.stroke();
              ctx.globalAlpha = 1;
            }
          }
        }

        // Ticket #14 FIX round (HIGH-1), re-tuned Ticket #16 FIX round:
        // a deterministic width-based truncation (`fitLabel`, applied to
        // `shortenLabel`'s output so a generic suffix drops before any
        // mid-word character truncation is needed), a token-colored
        // backing plate for legibility against edges/other nodes, and an
        // anchor placed *radially* outward from the node per
        // `labelDx`/`labelDy` — the offset pair `computeCuratedLayout`'s
        // greedy placer picked by checking each label's box against
        // every other curated label, which is what actually guarantees
        // no two curated labels overlap (see that function's docstring
        // for why a static above/below rule reads wrong once wedges are
        // angularly separated instead of stacked in columns).
        //
        // Ticket #16 (D-C5): at ~44 curated nodes, always-on labels for
        // everything reads as a wall of text — but the plan is explicit
        // that the hub, every financial asset, and any cascade-involved
        // node must stay permanently labelled regardless. Everything
        // else (low-criticality periphery — sensors, advisory feeds) is
        // hover-only, same mechanism the observed `/24` layer already
        // uses below. `labelDx`/`labelDy`/the collision-free box are
        // still computed for every node above (whether or not it ends up
        // drawn), so a node that only shows on hover still lands in its
        // pre-reserved, non-overlapping slot rather than fighting for
        // space live.
        const cascadeInvolved =
          !!cascade && (node.id === cascade.originAsset || cascade.impactedSet.has(node.id));
        const alwaysLabel =
          isHub ||
          node.isFinancial ||
          node.isGateway ||
          // Sector aggregate nodes: always-on regardless of criticality —
          // there are only ever ~11 of them in the default view, nowhere
          // near the density that made hover-only necessary at 44+ real
          // assets (D-R2's whole premise).
          node.isAggregate ||
          node.criticality >= ALWAYS_LABEL_CRITICALITY ||
          cascadeInvolved;
        if (alwaysLabel || hoveredNodeIdRef.current === node.id) {
          // The hub's label runs a size larger and in the brighter
          // `text` token (everything else uses `textDim`) — HIGH-1d
          // requires it stay "always-labelled with its full name," and a
          // visually louder label is part of what makes the centre read
          // as the hub rather than just another always-on label.
          const fontSize = isHub ? Math.max(13 / globalScale, 4) : Math.max(10 / globalScale, 3);
          ctx.font = `${fontSize}px ${monoFont}`;
          const label = fitLabel(ctx, shortenLabel(node.label), curatedLabelMaxWidthRef.current);
          const textWidth = ctx.measureText(label).width;
          const labelDx = node.labelDx ?? 0;
          const labelDy = node.labelDy ?? markerR + 3;
          const above = labelDy < 0;
          const anchorX = x + labelDx;
          const anchorY = y + labelDy;
          const platePadX = 3;
          const plateHeight = fontSize + 4;
          const plateTop = above ? anchorY - plateHeight : anchorY;

          // Phase 1 fix: `groundRaised` (#ffffff) on the canvas's own
          // white background is invisible — this token differs from the
          // panel behind it in a way `groundRaised` no longer does now
          // that panels are opaque white too. Full alpha (never fades —
          // labels are glyphs, not fills, per the Phase 1 rule) and a
          // stroke divided by `globalScale` so the hairline doesn't grow
          // into a heavy border at zoom (text itself is already held at
          // constant screen size via the `/globalScale` font calc above).
          ctx.fillStyle = colors.ground;
          ctx.fillRect(anchorX - textWidth / 2 - platePadX, plateTop, textWidth + platePadX * 2, plateHeight);
          ctx.lineWidth = 1 / globalScale;
          ctx.strokeStyle = isHub ? colors.graphAccentHi : colors.glassBorderStrong;
          ctx.strokeRect(anchorX - textWidth / 2 - platePadX, plateTop, textWidth + platePadX * 2, plateHeight);

          ctx.textAlign = "center";
          ctx.textBaseline = above ? "bottom" : "top";
          ctx.fillStyle = isHub ? colors.text : colors.textDim;
          ctx.fillText(label, anchorX, anchorY);
        }
      } else {
        // Observed `/24` cluster — hollow, dashed, muted. Must never
        // read as a curated asset (D11-1 / DESIGN_CONSOLE.md §6).
        const r = node.isOther ? 10 : Math.min(18, 5 + Math.log2(node.count + 1) * 2.2);
        ctx.save();
        ctx.setLineDash([3 / globalScale, 2.5 / globalScale]);
        ctx.beginPath();
        ctx.arc(x, y, r, 0, 2 * Math.PI);
        ctx.lineWidth = 1.3;
        ctx.strokeStyle = node.isOther ? colors.textMute : colors.sevInfo;
        ctx.globalAlpha = 1;
        ctx.stroke();
        ctx.restore();

        if (pulseColor) {
          const ringR = r + 4 + (reducedMotion ? 3 : pulseT * 6);
          ctx.beginPath();
          ctx.arc(x, y, ringR, 0, 2 * Math.PI);
          ctx.lineWidth = 2;
          ctx.strokeStyle = pulseColor;
          ctx.globalAlpha = reducedMotion ? 0.9 : 0.55 + (1 - pulseT) * 0.35;
          ctx.stroke();
          ctx.globalAlpha = 1;
        }

        // Ticket #14 (D14-1): cluster labels only on hover — always-on
        // labels for up to 24 arriving `/24`s is exactly what made the
        // graph illegible. Curated labels use the same hover fallback now
        // (Ticket #16, D-C5) for the low-criticality periphery only — the
        // hub, financial assets, gateways and cascade-involved nodes stay
        // permanently labelled above regardless of hover state.
        if (hoveredNodeIdRef.current === node.id) {
          const fontSize = Math.max(10 / globalScale, 3);
          ctx.font = `${fontSize}px ${monoFont}`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          // Phase 1 fix: `textMute` (~2.3:1 on white) was unreadable —
          // `textDim` (~8.6:1) matches every other hover/always-on label
          // on this canvas.
          ctx.fillStyle = colors.textDim;
          ctx.fillText(node.label, x, y + r + 3);
        }
      }
    },
    [colors, monoFont, reducedMotion, cascade, sectorMembers]
  );

  const linkColor = useCallback(
    (link: CityLinkDatum) => {
      if (link.layer === "curated" && cascade?.pathEdgeIds.has(link.id)) {
        const hop = cascade.edgeHopOf.get(link.id) ?? cascade.fallbackHop;
        const elapsed = reducedMotion ? Infinity : performance.now() - cascade.startedAt;
        if (elapsed >= hop * CASCADE_STAGGER_MS) return colors.sevCritical;
      }
      return link.layer === "curated"
        ? link.isGatewayEdge
          ? colors.graphAccent
          : colors.glassBorderStrong
        : colors.textMute;
    },
    [colors, cascade, reducedMotion]
  );

  const linkWidth = useCallback(
    (link: CityLinkDatum) => {
      if (link.layer === "curated" && cascade?.pathEdgeIds.has(link.id)) {
        const hop = cascade.edgeHopOf.get(link.id) ?? cascade.fallbackHop;
        const elapsed = reducedMotion ? Infinity : performance.now() - cascade.startedAt;
        if (elapsed >= hop * CASCADE_STAGGER_MS) return 2.6;
      }
      if (link.layer === "curated") {
        return link.isGatewayEdge ? 1.2 : 1.0;
      }
      // Aggregated `/24` edges scale by their own `count` — recessive
      // edges (Phase 1) still need to distinguish a single-flow link from
      // a thousand-flow one, which a flat width would erase.
      return Math.min(2.2, 0.4 + Math.log2(link.count + 1) * 0.3);
    },
    [cascade, reducedMotion]
  );

  const linkLineDash = useCallback(
    (link: CityLinkDatum) =>
      link.layer === "observed" || (link.layer === "curated" && link.isAggregate) ? [2, 2] : null,
    []
  );

  const nodeLabel = useCallback((node: CityNodeDatum) => {
    if (node.layer === "curated") {
      if (node.isAggregate) {
        return `${node.label} — sector · ${node.memberCount} asset${node.memberCount === 1 ? "" : "s"} · click to expand`;
      }
      const kind = node.isGateway ? "Gateway / chokepoint" : node.nodeType ?? "Asset";
      return `${node.label} — ${kind} · criticality ${node.criticality.toFixed(2)}`;
    }
    return node.isOther
      ? `${node.label} (rolled up, below cap)`
      : `${node.label} — observed flow count`;
  }, []);

  // Phase 2 (HIGH — every node was previously a ~4px click target
  // regardless of drawn size, react-force-graph's default `val`).
  // `drawnRadius` is the single source both `nodeVal` (physics-safe,
  // `nodeVal`/`triggerUpdate` is read by no d3 force) and
  // `nodePointerAreaPaint` (the actual hit region) derive from, so a
  // click target always matches what's on screen — including sector
  // aggregates, whose primary interaction IS click-to-expand.
  const drawnRadius = useCallback((node: CityNodeDatum): number => {
    if (node.layer === "curated") {
      return curatedMarkerRadius({
        id: node.id,
        criticality: node.criticality,
        isAggregate: node.isAggregate,
        isGateway: node.isGateway,
      });
    }
    return node.isOther ? 10 : Math.min(18, 5 + Math.log2(node.count + 1) * 2.2);
  }, []);

  const nodeVal = useCallback(
    (node: CityNodeDatum) => (drawnRadius(node) / NODE_REL_SIZE) ** 2,
    [drawnRadius]
  );

  const nodePointerAreaPaint = useCallback(
    (node: CityNodeDatum, color: string, ctx: CanvasRenderingContext2D) => {
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, drawnRadius(node) + 4, 0, 2 * Math.PI);
      ctx.fill();
    },
    [drawnRadius]
  );

  const clusterNodeCount = graphData.nodes.filter(
    (n) => n.layer === "observed" && !n.isOther
  ).length;
  const activeSectorCount = [...sectorMembers.keys()].filter(
    (s) => (sectorMembers.get(s)?.length ?? 0) > 0
  ).length;
  // D-R3: legend/status line extended with sector count + aggregated vs.
  // expanded state, so the view mode is always legible without having to
  // infer it from node count alone. Focus is stackable, so this handles
  // 0, 1, or many focused sectors — spelling out names only up to a point
  // before the line would get unreasonably long.
  const focusedSectorList = [...focusedSectors];
  const viewModeLabel = expanded
    ? "expanded · all assets"
    : focusedSectorList.length === 0
      ? "sector view"
      : focusedSectorList.length <= 2
        ? `sector view · ${focusedSectorList.map(sectorLabel).join(", ")} focused`
        : `sector view · ${focusedSectorList.length} sectors focused`;

  return (
    <div className="flex h-full min-h-0 w-full flex-col gap-2 overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[10px] uppercase tracking-[0.08em] text-text-mute">
        <Legend colors={colors} />
        <span className="font-mono normal-case tracking-normal text-text-dim">
          {topology.nodes.length} curated &middot; {activeSectorCount} sectors &middot;{" "}
          {clusterNodeCount} /24 clusters &middot; {viewModeLabel}
          {status !== "connected" ? ` · stream ${status}` : ""}
        </span>
      </div>
      {cascade && (
        // Ticket #14 (D14-2): CII is reported as a distribution, not a
        // point estimate (core project claim) — median AND the p5-p95
        // interval are both shown, never the median alone.
        <p className="text-[11px] normal-case leading-snug text-text-dim" role="status">
          <span className="font-semibold text-sev-critical">CII cascade</span>{" "}
          from <span className="font-mono">{cascade.originAsset}</span>: median{" "}
          <span className="font-mono tabular-nums">{cascade.ciiMedian.toFixed(1)}</span>{" "}
          (p5&ndash;p95{" "}
          <span className="font-mono tabular-nums">
            {cascade.ciiP5.toFixed(1)}&ndash;{cascade.ciiP95.toFixed(1)}
          </span>
          ) &middot; {cascade.impacted.length} impacted asset
          {cascade.impacted.length === 1 ? "" : "s"}
        </p>
      )}
      {/* `relative` + the graph wrapped in `absolute inset-0` below is
          deliberate, not decorative: react-force-graph sets the canvas's
          pixel width/height directly from the `width`/`height` props, and
          without taking it out of normal flow its explicit size feeds
          back into this container's own content-box height. Every
          ~100ms tick (D11-2's throttled cluster update) re-triggers
          layout, and on a flex chain that has no hard viewport-capped
          ancestor (this app's `body` only sets `min-height`, not a
          ceiling) that turns into a runaway feedback loop — measured in
          dev: the canvas grew from ~500px to >5500px tall within a few
          seconds of real replay traffic before this was fixed.
          Absolute positioning breaks the loop unconditionally, so it
          holds regardless of what any ancestor's height rules are. */}
      <div ref={containerRef} className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
        {size.width > 0 && size.height > 0 ? (
          <div className="absolute inset-0">
            <ForceGraph2D<CityNodeDatum, CityLinkDatum>
              ref={fgRef}
              graphData={graphData}
              width={size.width}
              height={size.height}
              backgroundColor="transparent"
              nodeCanvasObject={nodeCanvasObject}
              nodeCanvasObjectMode={() => "replace"}
              nodeLabel={nodeLabel}
              nodeRelSize={NODE_REL_SIZE}
              nodeVal={nodeVal}
              nodePointerAreaPaint={nodePointerAreaPaint}
              linkColor={linkColor}
              linkWidth={linkWidth}
              linkLineDash={linkLineDash}
              linkLabel={(l: CityLinkDatum) =>
                l.layer === "curated"
                  ? l.isAggregate
                    ? `${l.source} → ${l.target} · ${l.count} real edge${l.count === 1 ? "" : "s"} collapsed`
                    : `${l.source} → ${l.target} (${l.edgeType})`
                  : `${l.source} → ${l.target} · ${l.count} flows`
              }
              cooldownTicks={200}
              // 0, not a synchronous burst: D11-2 says "mutate node/link
              // arrays and let it settle," and every call to
              // `setGraphData` re-registers nodes/links with d3-force
              // (`resetCountdown`), which restarts the simulation's
              // alpha/tick counter. A nonzero `warmupTicks` would force a
              // large synchronous physics step on every one of those
              // resets; at high discovery rates (new `/24`s pushing
              // `structureChanged`, see the throttled tick above) that
              // repeated forced stepping is what drove positions to NaN
              // during testing. Zero warmup means new nodes just ease in
              // over the normal animation loop instead.
              warmupTicks={0}
              autoPauseRedraw={false}
              enableNodeDrag={true}
              onNodeHover={(node) => {
                hoveredNodeIdRef.current = node ? String(node.id) : null;
              }}
              onZoomEnd={() => {
                if (programmaticZoomCountRef.current === 0) userFramedRef.current = true;
              }}
              // D-R2 "clicking a sector expands that sector inline":
              // clicking a sector aggregate node toggles it into/out of
              // `focusedSectors` — stackable, so a second sector click
              // adds alongside whatever's already focused rather than
              // replacing it; clicking the hub while anything is focused
              // clears all of them, returning to the fully aggregated
              // view. Only meaningful in the default (non-expanded) view —
              // in the expanded view every real asset is already shown, so
              // there is no aggregate node to click.
              onNodeClick={(node) => {
                if (expanded || node.layer !== "curated") return;
                const id = String(node.id);
                if (node.isAggregate) {
                  const key = id.startsWith("sector:") ? id.slice("sector:".length) : null;
                  if (key) toggleFocusedSector(key);
                } else if (id === HUB_ASSET_NAME && focusedSectors.size > 0) {
                  clearFocusedSectors();
                }
              }}
            />
          </div>
        ) : (
          // Never render nothing (DESIGN_CONSOLE.md §6 — "never render a
          // blank panel"): if the holder hasn't reported a usable size
          // yet (still mid-measurement, or the bounded rAF retry loop in
          // `useContainerSize` hasn't landed a non-zero rect), say so
          // honestly instead of leaving an empty div. This is expected to
          // be visible for at most a frame or two on a normal mount.
          <div className="absolute inset-0 flex items-center justify-center text-[11px] uppercase tracking-[0.08em] text-text-mute">
            sizing graph&hellip;
          </div>
        )}
      </div>
      <p className="text-[11px] leading-snug text-text-mute" role="status">
        {caption}
      </p>
    </div>
  );
}

function Legend({ colors }: { colors: ReturnType<typeof useThemeColors> }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <LegendItem
        swatch={
          <span className="relative inline-flex h-3.5 w-3.5 items-center justify-center rounded-full" style={{ background: colors.graphAccentHi, boxShadow: `0 0 0 1.5px ${colors.text}` }}>
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: colors.ground }} />
          </span>
        }
        label="Hub"
      />
      <LegendItem swatch={<span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: colors.graphAccent }} />} label="Infra" />
      <LegendItem
        swatch={
          <span
            className="inline-block h-2.5 w-2.5"
            style={{ background: colors.financial, clipPath: "polygon(50% 0, 100% 50%, 50% 100%, 0 50%)" }}
          />
        }
        label="Financial"
      />
      <LegendItem
        swatch={<span className="inline-block h-3 w-3 rounded-full border-[1.5px]" style={{ borderColor: colors.graphAccentHi }} />}
        label="Gateway"
      />
      <LegendItem
        swatch={
          <span
            className="inline-block h-3 w-3 rounded-full"
            style={{ background: colors.graphAccent, boxShadow: `0 0 0 1.5px ${colors.graphAccentHi}` }}
          />
        }
        label="Sector (click to expand)"
      />
      <LegendItem
        swatch={<span className="inline-block h-2.5 w-2.5 rounded-full border border-dashed" style={{ borderColor: colors.sevInfo }} />}
        label="/24 cluster"
      />
    </div>
  );
}

function LegendItem({ swatch, label }: { swatch: ReactNode; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      {swatch}
      {label}
    </span>
  );
}
