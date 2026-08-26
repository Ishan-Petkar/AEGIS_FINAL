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

/** Deterministic-ish seed so re-mounts don't jump nodes around wildly; layer bias keeps the two layers visually apart from the start. */
function seedPosition(width: number, height: number, side: "left" | "right") {
  const w = width || 600;
  const h = height || 400;
  const xBase = side === "left" ? w * 0.28 : w * 0.72;
  return {
    x: xBase + (Math.random() - 0.5) * w * 0.3,
    y: h / 2 + (Math.random() - 0.5) * h * 0.6,
  };
}

export function CityGraph({ topology }: { topology: TopologyResponse }) {
  const { status, events } = useStream();
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
  // both of which fire `onZoomEnd` — see the reframe effects below.
  const programmaticZoomRef = useRef(false);
  const userFramedRef = useRef(false);
  const reframe = useCallback((duration: number) => {
    programmaticZoomRef.current = true;
    fgRef.current?.zoomToFit(duration, 40);
    window.setTimeout(() => {
      programmaticZoomRef.current = false;
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

  // (Re)seed the curated layer whenever topology changes (mount, or a
  // manual retry after a topology fetch failure). This runs once per
  // topology load, not on the render-interval tick.
  useEffect(() => {
    const nodesMap = nodesMapRef.current;
    const linksMap = linksMapRef.current;
    for (const n of topology.nodes) {
      const existing = nodesMap.get(n.name) as CuratedNodeDatum | undefined;
      const isFinancial = (n.type ?? "").includes("Financial");
      if (existing && existing.layer === "curated") {
        existing.nodeType = n.type;
        existing.criticality = n.criticality;
        existing.isGateway = n.is_gateway;
        existing.isFinancial = isFinancial;
      } else {
        const pos = seedPosition(sizeRef.current.width, sizeRef.current.height, "left");
        nodesMap.set(n.name, {
          id: n.name,
          layer: "curated",
          label: n.name,
          nodeType: n.type,
          criticality: n.criticality,
          isGateway: n.is_gateway,
          isFinancial,
          pulseSeverity: "normal",
          ...pos,
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
  }, [topology]);

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
        const baseR = 3 + node.criticality * 7;
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

        const fontSize = Math.max(11 / globalScale, 3.2);
        ctx.font = `${fontSize}px ${monoFont}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = colors.textDim;
        ctx.fillText(node.label, x, y + baseR + 3);
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

        const fontSize = Math.max(10 / globalScale, 3);
        ctx.font = `${fontSize}px ${monoFont}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = colors.textMute;
        ctx.fillText(node.label, x, y + r + 3);
      }
    },
    [colors, monoFont, reducedMotion]
  );

  const linkColor = useCallback(
    (link: CityLinkDatum) =>
      link.layer === "curated" ? (link.isGatewayEdge ? colors.accent : colors.textDim) : colors.textMute,
    [colors]
  );

  const linkWidth = useCallback(
    (link: CityLinkDatum) =>
      link.layer === "curated" ? 1.4 : Math.min(2.2, 0.4 + Math.log2(link.count + 1) * 0.3),
    []
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
              onZoomEnd={() => {
                if (!programmaticZoomRef.current) userFramedRef.current = true;
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
