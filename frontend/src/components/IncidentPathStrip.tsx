"use client";

/**
 * IncidentPathStrip — linear left-to-right chip strip showing the active
 * incident path: External Network → Gateway → [origin ●] → hop-1 → hop-2…
 *
 * Uses the same `latestCii` stream state the graph consumes, and the same
 * `computeCascadeGeometry` from `@/lib/cascade` (single implementation,
 * no drift). Hop-staggered reveal matches CityGraph's CASCADE_STAGGER_MS.
 *
 * Phase 5 — IncidentPathStrip.
 */

import { useEffect, useMemo, useState } from "react";
import { useStream } from "@/lib/stream-context";
import { useTopology } from "@/lib/topology-context";
import { computeCascadeGeometry } from "@/lib/cascade";
import type { CiiEnvelopeData } from "@/lib/types";

// Must stay in sync with CityGraph's constant (frontend presentation timing).
const CASCADE_STAGGER_MS = 260;
// How long (ms) after the last cascade to hold the strip before fading out.
const CASCADE_HOLD_MS = 12_000;

interface PathNode {
  id: string;
  label: string;
  role: "external" | "gateway" | "origin" | "impacted" | "normal";
  hop: number;
}

function GlobeIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" aria-hidden="true">
      <circle cx="10" cy="10" r="7.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M10 2.5c-2.5 2.5-2.5 12.5 0 15M10 2.5c2.5 2.5 2.5 12.5 0 15M2.5 10h15" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

function RouterIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" aria-hidden="true">
      <rect x="2" y="7" width="16" height="8" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 7V5M10 7V4M14 7V5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="6" cy="12" r="1.2" fill="currentColor" />
      <circle cx="10" cy="12" r="1.2" fill="currentColor" />
    </svg>
  );
}

function AlertHexIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" aria-hidden="true">
      <path d="M10 2 18 7v6l-8 5-8-5V7z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M10 8v3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="10" cy="13.5" r="0.8" fill="currentColor" />
    </svg>
  );
}

function NodeIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" aria-hidden="true">
      <circle cx="10" cy="10" r="6" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="10" cy="10" r="2.5" fill="currentColor" opacity="0.5" />
    </svg>
  );
}

function ArrowRight() {
  return (
    <svg viewBox="0 0 20 8" width="18" height="8" fill="none" aria-hidden="true" className="shrink-0">
      <path d="M0 4h16M13 1l3 3-3 3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function PathChip({ node, visible }: { node: PathNode; visible: boolean }) {
  const isOrigin = node.role === "origin";
  const isImpacted = node.role === "impacted";
  const isExternal = node.role === "external";
  const isGateway = node.role === "gateway";

  const bgClass = isOrigin
    ? "bg-sev-critical/10 border-sev-critical text-sev-critical"
    : isImpacted
      ? "bg-sev-warning/10 border-sev-warning text-sev-warning"
      : isExternal
        ? "bg-accent/10 border-accent text-accent"
        : isGateway
          ? "bg-accent/8 border-accent/60 text-accent"
          : "bg-glass-raised border-glass-border-strong text-text-dim";

  const Icon = isExternal ? GlobeIcon : isGateway ? RouterIcon : isOrigin ? AlertHexIcon : NodeIcon;

  return (
    <div
      className={`flex items-center gap-1.5 rounded-[var(--radius-dense)] border px-2.5 py-1 text-[11px] font-semibold transition-all duration-500 ${bgClass} ${
        visible ? "opacity-100 translate-x-0" : "opacity-0 -translate-x-2"
      }`}
      aria-label={`${node.role === "origin" ? "Attack origin" : node.role === "impacted" ? "Impacted asset" : "Path node"}: ${node.label}`}
    >
      <Icon />
      <span className="max-w-[120px] truncate">{node.label}</span>
      {isOrigin && (
        <span className="ml-0.5 rounded-full bg-sev-critical px-1 text-[9px] font-bold text-white" aria-label="Compromised">
          !
        </span>
      )}
    </div>
  );
}

/** Short label: strip known prefixes and keep the meaningful part. */
function shortLabel(name: string): string {
  return name
    .replace(/^Unresolved_/, "")
    .replace(/^City_/, "")
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function IncidentPathStrip() {
  const { latestCii } = useStream();
  const { topology } = useTopology();
  const [elapsed, setElapsed] = useState(0);
  const [activeCii, setActiveCii] = useState<CiiEnvelopeData | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);

  // Track the latest non-null latestCii + its arrival time.
  useEffect(() => {
    if (!latestCii) return;
    setActiveCii(latestCii);
    setStartedAt(performance.now());
    setElapsed(0);
  }, [latestCii]);

  // Tick elapsed so chips reveal with the same stagger as the graph.
  useEffect(() => {
    if (startedAt === null) return;
    const id = setInterval(() => {
      setElapsed(performance.now() - startedAt);
    }, 80);
    return () => clearInterval(id);
  }, [startedAt]);

  const pathNodes = useMemo((): PathNode[] => {
    if (!activeCii || !topology) return [];
    const impacted = Array.isArray(activeCii.impacted)
      ? activeCii.impacted.filter((v): v is string => typeof v === "string")
      : [];

    const { linearPath, hopOf } = computeCascadeGeometry(
      activeCii.origin_asset,
      impacted,
      topology.edges
    );

    // Find gateway(s): nodes where is_gateway = true.
    const gatewayNames = new Set(topology.nodes.filter((n) => n.is_gateway).map((n) => n.name));

    // Build ordered chip list: External → Gateway (if any on path) → real path.
    const result: PathNode[] = [];

    // "External Network" pseudo-node — always first.
    result.push({ id: "__external__", label: "External Network", role: "external", hop: -2 });

    // If there is a gateway asset on the path or topology, insert it.
    const pathSet = new Set(linearPath);
    const pathGateway = linearPath.find((n) => gatewayNames.has(n));
    const anyGateway = [...gatewayNames][0];
    const gw = pathGateway ?? anyGateway;
    if (gw && !pathSet.has(gw)) {
      result.push({ id: gw, label: shortLabel(gw), role: "gateway", hop: -1 });
    }

    // Real path nodes.
    for (const name of linearPath) {
      const isOrigin = name === activeCii.origin_asset;
      const isImpacted = !isOrigin && impacted.includes(name);
      const isGateway = gatewayNames.has(name);
      result.push({
        id: name,
        label: shortLabel(name),
        role: isOrigin ? "origin" : isGateway ? "gateway" : isImpacted ? "impacted" : "normal",
        hop: hopOf.get(name) ?? 0,
      });
    }

    return result;
  }, [activeCii, topology]);

  // Fade out after CASCADE_HOLD_MS of no new CII event.
  const isFaded = startedAt !== null && elapsed > CASCADE_HOLD_MS;

  if (pathNodes.length === 0) {
    return (
      <div className="glass-panel flex h-10 items-center gap-2 px-4 text-[11px] text-text-mute">
        <svg viewBox="0 0 20 20" width="14" height="14" fill="none" aria-hidden="true" className="shrink-0">
          <circle cx="10" cy="10" r="7.5" stroke="currentColor" strokeWidth="1.5" />
          <path d="M10 7v3M10 13h.01" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        No active incident — path appears when a compromise is detected
      </div>
    );
  }

  return (
    <div
      className={`glass-panel flex items-center gap-0 overflow-x-auto px-3 py-2 transition-opacity duration-700 ${isFaded ? "opacity-30" : "opacity-100"}`}
      role="region"
      aria-label="Incident propagation path"
    >
      <span className="mr-2 shrink-0 text-[9px] font-semibold uppercase tracking-[0.08em] text-text-mute">
        Incident path
      </span>
      <div className="flex min-w-0 items-center gap-1.5">
        {pathNodes.map((node, idx) => {
          // Chips reveal staggered: external/gateway show immediately,
          // real nodes reveal at their hop * CASCADE_STAGGER_MS.
          const revealMs = node.hop < 0 ? 0 : node.hop * CASCADE_STAGGER_MS;
          const visible = elapsed >= revealMs;
          return (
            <div key={node.id} className="flex shrink-0 items-center gap-1.5">
              {idx > 0 && (
                <span className={`text-text-mute transition-opacity duration-300 ${visible ? "opacity-100" : "opacity-0"}`}>
                  <ArrowRight />
                </span>
              )}
              <PathChip node={node} visible={visible} />
            </div>
          );
        })}
      </div>
      {activeCii && (
        <span className="ml-auto shrink-0 pl-4 font-mono text-[10px] tabular-nums text-text-mute">
          CII {(activeCii.cii_median * 100).toFixed(0)}%
        </span>
      )}
    </div>
  );
}
