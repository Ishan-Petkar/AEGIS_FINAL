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

interface CuratedNodeDatum {
  id: string;
  layer: "curated";
  label: string;
  nodeType: string | null;
  criticality: number;
  isGateway: boolean;
  isFinancial: boolean;
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
  // Ticket #14 FIX round (HIGH-1): the label's vertical offset from the
  // node's own y, in the same units as x/y — negative draws the label
  // above the node, positive below. Assigned once by
  // `computeCuratedLayout`'s greedy label placer, which checks each
  // node's candidate box against every already-placed label to
  // guarantee no two curated labels overlap (see that function's
  // docstring for why a static above/below-by-column rule wasn't
  // enough).
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

/** Deterministic-ish seed so re-mounts don't jump nodes around wildly; layer bias keeps the two layers visually apart from the start. Used for the cluster (observed) layer only — see `computeCuratedLayout` for the curated layer, which is pinned rather than seeded. */
function seedPosition(width: number, height: number, side: "left" | "right") {
  const w = width || 600;
  const h = height || 400;
  const xBase = side === "left" ? w * 0.28 : w * 0.72;
  return {
    x: xBase + (Math.random() - 0.5) * w * 0.3,
    y: h / 2 + (Math.random() - 0.5) * h * 0.6,
  };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/** Shared by layout (approximate, pre-measurement) and rendering (exact) so the two never disagree about how much room a node's marker needs. */
function curatedNodeRadius(criticality: number): number {
  return 3 + criticality * 7;
}

/**
 * Ticket #14 FIX round (HIGH-1): average y-rank (0..1) of `name`'s
 * neighbours that have already been placed (i.e. sit in an earlier,
 * already-processed column). Returns `null` when none have — the first
 * column, and any node whose only neighbours are further right. Used by
 * `computeCuratedLayout`'s single left-to-right barycenter sweep so
 * dependency edges pull toward similar y instead of a naive
 * criticality-only ordering, which is what produced the long,
 * near-vertical edges HIGH-1 also flagged.
 */
function barycenterOf(
  name: string,
  neighborsOf: Map<string, string[]>,
  yRankOf: Map<string, number>
): number | null {
  const neighbors = neighborsOf.get(name);
  if (!neighbors || neighbors.length === 0) return null;
  let sum = 0;
  let count = 0;
  for (const nb of neighbors) {
    const r = yRankOf.get(nb);
    if (r !== undefined) {
      sum += r;
      count += 1;
    }
  }
  return count > 0 ? sum / count : null;
}

/**
 * Ticket #14 (D14-1), reworked in the FIX round for HIGH-1: a stable,
 * deterministic layout for the 16 curated nodes, computed by Purdue
 * level (columns, left-to-right by ascending level; the one node with
 * `purdue_level: null` — `City_Grid`, a synthesized node — gets its own
 * trailing column). This replaces physics-derived seeding for the
 * curated layer entirely: the topology is a fixed, known structure, so
 * there is nothing for a force simulation to discover, and re-deriving
 * it every frame is exactly what let 24 arriving clusters shove the
 * curated layer into a knot (see the ticket plan). Callers pin the
 * returned positions via `fx`/`fy`.
 *
 * HIGH-1 (FIX round): the original version ordered each column
 * independently by criticality and spread it evenly over the *same*
 * `[0,1]` fraction of `rowSpan` used by every other column. With
 * columns only ~35-45px apart and labels routinely 100-150px wide, that
 * made most columns share a handful of identical y-heights (row 0, the
 * middle, the last row), so at default zoom whole horizontal bands of
 * unrelated labels from different columns landed on top of each other
 * — exactly the jumbled overlap in the ticket's screenshot. Two changes
 * fix it without touching which nodes exist, which column they sit in,
 * or the two-layer separation:
 *
 * 1. A single left-to-right barycenter sweep (`barycenterOf`) orders
 *    each column by the average y-rank of its already-placed neighbours
 *    instead of criticality alone, which shortens the long near-vertical
 *    dependency edges and, as a side effect, decorrelates which node
 *    ends up in which row per column versus a fixed rule.
 * 2. A deterministic per-column phase offset (`colIdx % 4`) shifts each
 *    column's row band within `rowSpan`, so columns of equal node count
 *    no longer land on the exact same fractional y positions.
 * 3. A greedy sequential label placer (below, processing nodes
 *    left-to-right) picks each label's vertical offset — above or below
 *    the node, at increasing distance rings — by checking it against
 *    *every* already-placed label's box, not just its column neighbours.
 *    This is the part that actually guarantees no overlap: (1) and (2)
 *    reduce how often two labels land close together, but an
 *    alternate-by-column-parity rule (tried first, dropped) still failed
 *    whenever the "below" node happened to sit *above* the "above" node
 *    for the same pair — both labels then aim at each other into a gap
 *    too narrow for either. Checking real computed boxes instead of a
 *    static parity rule closes that hole regardless of relative y.
 *
 * Still confined to the left ~5%-48% of the container width so it never
 * overlaps the cluster layer's seed region (`seedPosition`'s "right"
 * side starts at 0.72w with a 0.15w spread, i.e. never below ~0.57w) or
 * the `clusterConfineForce` containment boundary below (0.52w).
 */
function computeCuratedLayout(
  nodes: TopologyResponse["nodes"],
  edges: TopologyResponse["edges"],
  width: number,
  height: number
): { positions: Map<string, { x: number; y: number; labelDy: number }>; labelMaxWidth: number } {
  const w = width || 600;
  const h = height || 400;
  const NULL_LEVEL = 6; // sorts after real Purdue levels 0-5
  const levelOf = (lvl: number | null) => (lvl === null ? NULL_LEVEL : lvl);

  const byLevel = new Map<number, TopologyResponse["nodes"]>();
  for (const n of nodes) {
    const lvl = levelOf(n.purdue_level);
    const list = byLevel.get(lvl);
    if (list) list.push(n);
    else byLevel.set(lvl, [n]);
  }
  const levels = [...byLevel.keys()].sort((a, b) => a - b);

  const leftMargin = w * 0.05;
  const columnSpan = w * 0.43;
  const topMargin = h * 0.1;
  const rowSpan = h * 0.8;

  const neighborsOf = new Map<string, string[]>();
  for (const e of edges) {
    if (!neighborsOf.has(e.source)) neighborsOf.set(e.source, []);
    if (!neighborsOf.has(e.target)) neighborsOf.set(e.target, []);
    neighborsOf.get(e.source)!.push(e.target);
    neighborsOf.get(e.target)!.push(e.source);
  }
  const yRankOf = new Map<string, number>();

  const basePositions = new Map<string, { x: number; y: number }>();
  levels.forEach((lvl, colIdx) => {
    const x =
      levels.length > 1
        ? leftMargin + (colIdx / (levels.length - 1)) * columnSpan
        : leftMargin + columnSpan / 2;
    const colNodes = [...(byLevel.get(lvl) ?? [])].sort((a, b) => {
      const aBary = barycenterOf(a.name, neighborsOf, yRankOf);
      const bBary = barycenterOf(b.name, neighborsOf, yRankOf);
      if (aBary !== null && bBary !== null && aBary !== bBary) return aBary - bBary;
      if (aBary !== null && bBary === null) return -1;
      if (bBary !== null && aBary === null) return 1;
      if (b.criticality !== a.criticality) return b.criticality - a.criticality;
      return a.name.localeCompare(b.name);
    });

    const phase = (colIdx % 4) / 4; // 0, .25, .5, .75, repeating
    const usableSpan = rowSpan * 0.8;
    const phaseOffset = phase * rowSpan * 0.2;

    colNodes.forEach((n, rowIdx) => {
      const frac = colNodes.length > 1 ? rowIdx / (colNodes.length - 1) : 0.5;
      const y = topMargin + phaseOffset + frac * usableSpan;
      yRankOf.set(n.name, frac);
      basePositions.set(n.name, { x, y });
    });
  });

  // Per-node label budget in screen px: labels are drawn at a constant
  // screen-space size regardless of zoom (`fontSize = C / globalScale`
  // below), and `zoomToFit` scales the whole two-layer graph to roughly
  // fill the container, so container-space px here is a good proxy for
  // eventual screen px. Sized off the actual column spacing rather than
  // a fixed constant so it adapts to container width; clamped so very
  // narrow or very wide containers still get a sane budget.
  const columnSpacing = levels.length > 1 ? columnSpan / (levels.length - 1) : columnSpan;
  const labelMaxWidth = clamp(columnSpacing * 2.3, 70, 150);

  // Greedy sequential label placement (HIGH-1): process nodes
  // left-to-right (then top-to-bottom), and for each pick the nearest
  // above/below ring whose label box doesn't intersect any box already
  // placed for an earlier node. `CHAR_WIDTH_PX` is a monospace estimate
  // at the ~10px render font — it doesn't need to be exact, only
  // consistent enough that boxes computed here are a reasonable proxy
  // for what `fitLabel` + `ctx.measureText` actually draw later.
  const LABEL_HEIGHT_PX = 15;
  const CHAR_WIDTH_PX = 6;
  const MIN_GAP_PX = 5;
  const RING_COUNT = 5;
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
    return pa.x !== pb.x ? pa.x - pb.x : pa.y - pb.y;
  });
  const placedBoxes: Box[] = [];
  const positions = new Map<string, { x: number; y: number; labelDy: number }>();
  for (const n of order) {
    const pos = basePositions.get(n.name)!;
    const r = curatedNodeRadius(n.criticality);
    const halfWidth = Math.min(n.name.length * CHAR_WIDTH_PX, labelMaxWidth) / 2 + 3;
    let chosenDy = r + MIN_GAP_PX;
    let chosenBox: Box = {
      left: pos.x - halfWidth,
      right: pos.x + halfWidth,
      top: pos.y + chosenDy,
      bottom: pos.y + chosenDy + LABEL_HEIGHT_PX,
    };
    let placed = false;
    for (let ring = 0; ring < RING_COUNT && !placed; ring++) {
      const dist = r + MIN_GAP_PX + ring * (LABEL_HEIGHT_PX + 4);
      for (const dy of [dist, -dist]) {
        const box: Box =
          dy >= 0
            ? { left: pos.x - halfWidth, right: pos.x + halfWidth, top: pos.y + dy, bottom: pos.y + dy + LABEL_HEIGHT_PX }
            : { left: pos.x - halfWidth, right: pos.x + halfWidth, top: pos.y + dy - LABEL_HEIGHT_PX, bottom: pos.y + dy };
        if (!placedBoxes.some((b) => boxesOverlap(box, b))) {
          chosenDy = dy;
          chosenBox = box;
          placed = true;
          break;
        }
      }
    }
    placedBoxes.push(chosenBox);
    positions.set(n.name, { x: pos.x, y: pos.y, labelDy: chosenDy });
  }

  return { positions, labelMaxWidth };
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
 * name remains available via `nodeLabel` (hover tooltip).
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
 * drifting past the curated layer's right edge (~0.43w), not about
 * protecting the curated nodes a second time.
 */
function makeClusterConfineForce(
  nodesMapRef: { current: Map<string, CityNodeDatum> },
  sizeRef: { current: { width: number; height: number } }
) {
  return function clusterConfineForce(alpha: number) {
    const { width, height } = sizeRef.current;
    if (width <= 0 || height <= 0) return;
    const targetX = width * 0.72;
    const minX = width * 0.52;
    const topY = height * 0.06;
    const bottomY = height * 0.94;
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

  const knownAssets = useMemo(
    () => new Set(topology.nodes.map((n) => n.name)),
    [topology]
  );

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
  // create — the curated layer's whole point is that its 16 nodes sit at
  // a stable, known position rather than wherever physics leaves them.
  // Re-running on a real size change (which happens once or twice right
  // after mount, then never again barring a window resize) rescales the
  // deterministic grid; it never happens on the ~100ms cluster tick, so
  // it does not fight that effect's "don't reheat every tick" discipline.
  useEffect(() => {
    const nodesMap = nodesMapRef.current;
    const linksMap = linksMapRef.current;
    const { positions, labelMaxWidth } = computeCuratedLayout(
      topology.nodes,
      topology.edges,
      size.width,
      size.height
    );
    curatedLabelMaxWidthRef.current = labelMaxWidth;
    for (const n of topology.nodes) {
      const pos = positions.get(n.name) ?? {
        x: (size.width || 600) / 2,
        y: (size.height || 400) / 2,
        labelDy: 12,
      };
      const existing = nodesMap.get(n.name) as CuratedNodeDatum | undefined;
      const isFinancial = (n.type ?? "").includes("Financial");
      if (existing && existing.layer === "curated") {
        existing.nodeType = n.type;
        existing.criticality = n.criticality;
        existing.isGateway = n.is_gateway;
        existing.isFinancial = isFinancial;
        existing.x = pos.x;
        existing.y = pos.y;
        existing.fx = pos.x;
        existing.fy = pos.y;
        existing.labelDy = pos.labelDy;
      } else {
        nodesMap.set(n.name, {
          id: n.name,
          layer: "curated",
          label: n.name,
          nodeType: n.type,
          criticality: n.criticality,
          isGateway: n.is_gateway,
          isFinancial,
          pulseSeverity: "normal",
          x: pos.x,
          y: pos.y,
          fx: pos.x,
          fy: pos.y,
          labelDy: pos.labelDy,
        });
      }
    }
    for (const e of topology.edges) {
      const id = `curated:${e.source}->${e.target}`;
      if (!linksMap.has(id)) {
        linksMap.set(id, {
          id,
          source: e.source,
          target: e.target,
          layer: "curated",
          edgeType: e.edge_type,
          isGatewayEdge: e.is_gateway_edge,
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
  }, [topology, size.width, size.height, reducedMotion, reframe]);

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
      // `setGraphData` runs below.
      for (const node of nodesMap.values()) {
        if (node.layer === "curated") {
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
  }, [knownAssets]);

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
      startedAt: performance.now(),
    });
  }, [latestCii, topology.edges]);

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
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      const t = reducedMotion ? 0 : performance.now();
      const pulseT = (Math.sin(t / 420) + 1) / 2; // 0..1

      const pulseColor =
        node.pulseSeverity === "critical"
          ? colors.sevCritical
          : node.pulseSeverity === "warning"
            ? colors.sevWarning
            : null;

      if (node.layer === "curated") {
        const baseR = curatedNodeRadius(node.criticality);
        if (node.isGateway) {
          const r = Math.max(baseR, 9);
          ctx.beginPath();
          ctx.arc(x, y, r, 0, 2 * Math.PI);
          ctx.lineWidth = 1.6;
          ctx.strokeStyle = colors.accentHi;
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(x, y, r * 0.55, 0, 2 * Math.PI);
          ctx.fillStyle = colors.accent;
          ctx.globalAlpha = 0.55;
          ctx.fill();
          ctx.globalAlpha = 1;
        } else if (node.isFinancial) {
          const r = baseR;
          ctx.beginPath();
          ctx.moveTo(x, y - r);
          ctx.lineTo(x + r, y);
          ctx.lineTo(x, y + r);
          ctx.lineTo(x - r, y);
          ctx.closePath();
          ctx.fillStyle = colors.financial;
          ctx.fill();
        } else {
          ctx.beginPath();
          ctx.arc(x, y, baseR, 0, 2 * Math.PI);
          ctx.fillStyle = colors.accent;
          ctx.fill();
        }

        if (pulseColor) {
          const ringR = baseR + 4 + (reducedMotion ? 3 : pulseT * 6);
          ctx.beginPath();
          ctx.arc(x, y, ringR, 0, 2 * Math.PI);
          ctx.lineWidth = 2;
          ctx.strokeStyle = pulseColor;
          ctx.globalAlpha = reducedMotion ? 0.9 : 0.35 + (1 - pulseT) * 0.5;
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
            const ringR = baseR + 7 + (reducedMotion ? 4 : pulseT * 8);
            ctx.beginPath();
            ctx.arc(x, y, ringR, 0, 2 * Math.PI);
            ctx.lineWidth = 2.4;
            ctx.strokeStyle = colors.sevCritical;
            ctx.globalAlpha = reducedMotion ? 0.95 : 0.45 + (1 - pulseT) * 0.5;
            ctx.stroke();
            ctx.globalAlpha = 1;
          } else if (cascade.impactedSet.has(node.id)) {
            const hop = cascade.hopOf.get(node.id) ?? cascade.fallbackHop;
            const elapsed = reducedMotion ? Infinity : performance.now() - cascade.startedAt;
            if (elapsed >= hop * CASCADE_STAGGER_MS) {
              const ringR = baseR + 5;
              ctx.beginPath();
              ctx.arc(x, y, ringR, 0, 2 * Math.PI);
              ctx.lineWidth = 2;
              ctx.strokeStyle = colors.sevWarning;
              ctx.globalAlpha = 0.85;
              ctx.stroke();
              ctx.globalAlpha = 1;
            }
          }
        }

        // Ticket #14 FIX round (HIGH-1): smaller font, a deterministic
        // width-based truncation (`fitLabel`), a token-colored backing
        // plate for legibility against edges/other nodes, and drawing
        // above vs. below the node per `labelDy` — the signed offset
        // `computeCuratedLayout`'s greedy placer picked by checking each
        // label's box against every other curated label, which is what
        // actually guarantees no two curated labels overlap (a static
        // above/below-by-column rule was tried and dropped — see that
        // function's docstring).
        const fontSize = Math.max(10 / globalScale, 3);
        ctx.font = `${fontSize}px ${monoFont}`;
        const label = fitLabel(ctx, node.label, curatedLabelMaxWidthRef.current);
        const textWidth = ctx.measureText(label).width;
        const labelDy = node.labelDy ?? baseR + 3;
        const above = labelDy < 0;
        const anchorY = y + labelDy;
        const platePadX = 3;
        const plateHeight = fontSize + 4;
        const plateTop = above ? anchorY - plateHeight : anchorY;

        ctx.fillStyle = colors.groundRaised;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x - textWidth / 2 - platePadX, plateTop, textWidth + platePadX * 2, plateHeight);
        ctx.globalAlpha = 1;
        ctx.lineWidth = 1;
        ctx.strokeStyle = colors.glassBorder;
        ctx.strokeRect(x - textWidth / 2 - platePadX, plateTop, textWidth + platePadX * 2, plateHeight);

        ctx.textAlign = "center";
        ctx.textBaseline = above ? "bottom" : "top";
        ctx.fillStyle = colors.textDim;
        ctx.fillText(label, x, anchorY);
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
        ctx.globalAlpha = 0.8;
        ctx.stroke();
        ctx.restore();

        if (pulseColor) {
          const ringR = r + 4 + (reducedMotion ? 3 : pulseT * 6);
          ctx.beginPath();
          ctx.arc(x, y, ringR, 0, 2 * Math.PI);
          ctx.lineWidth = 2;
          ctx.strokeStyle = pulseColor;
          ctx.globalAlpha = reducedMotion ? 0.9 : 0.35 + (1 - pulseT) * 0.5;
          ctx.stroke();
          ctx.globalAlpha = 1;
        }

        // Ticket #14 (D14-1): cluster labels only on hover — always-on
        // labels for up to 24 arriving `/24`s is exactly what made the
        // graph illegible. Curated labels (above) always show; they're
        // a fixed, sparse 16 nodes at deterministic positions.
        if (hoveredNodeIdRef.current === node.id) {
          const fontSize = Math.max(10 / globalScale, 3);
          ctx.font = `${fontSize}px ${monoFont}`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = colors.textMute;
          ctx.fillText(node.label, x, y + r + 3);
        }
      }
    },
    [colors, monoFont, reducedMotion, cascade]
  );

  const linkColor = useCallback(
    (link: CityLinkDatum) => {
      if (link.layer === "curated" && cascade?.pathEdgeIds.has(link.id)) {
        const hop = cascade.edgeHopOf.get(link.id) ?? cascade.fallbackHop;
        const elapsed = reducedMotion ? Infinity : performance.now() - cascade.startedAt;
        if (elapsed >= hop * CASCADE_STAGGER_MS) return colors.sevCritical;
      }
      return link.layer === "curated" ? (link.isGatewayEdge ? colors.accent : colors.textDim) : colors.textMute;
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
      return link.layer === "curated" ? 1.4 : Math.min(2.2, 0.4 + Math.log2(link.count + 1) * 0.3);
    },
    [cascade, reducedMotion]
  );

  const linkLineDash = useCallback(
    (link: CityLinkDatum) => (link.layer === "observed" ? [2, 2] : null),
    []
  );

  const nodeLabel = useCallback((node: CityNodeDatum) => {
    if (node.layer === "curated") {
      const kind = node.isGateway ? "Gateway / chokepoint" : node.nodeType ?? "Asset";
      return `${node.label} — ${kind} · criticality ${node.criticality.toFixed(2)}`;
    }
    return node.isOther
      ? `${node.label} (rolled up, below cap)`
      : `${node.label} — observed flow count`;
  }, []);

  const clusterNodeCount = graphData.nodes.filter(
    (n) => n.layer === "observed" && !n.isOther
  ).length;

  return (
    <div className="flex h-full min-h-0 w-full flex-col gap-2 overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[10px] uppercase tracking-[0.08em] text-text-mute">
        <Legend colors={colors} />
        <span className="font-mono normal-case tracking-normal text-text-dim">
          {topology.nodes.length} curated &middot; {clusterNodeCount} /24 clusters
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
              linkColor={linkColor}
              linkWidth={linkWidth}
              linkLineDash={linkLineDash}
              linkLabel={(l: CityLinkDatum) =>
                l.layer === "curated" ? `${l.source} → ${l.target} (${l.edgeType})` : `${l.source} → ${l.target} · ${l.count} flows`
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
      <LegendItem swatch={<span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: colors.accent }} />} label="Infra" />
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
        swatch={<span className="inline-block h-3 w-3 rounded-full border-[1.5px]" style={{ borderColor: colors.accentHi }} />}
        label="Gateway"
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
