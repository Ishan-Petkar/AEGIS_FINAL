"use client";

import { createContext, useContext, useState, type ReactNode } from "react";

/**
 * GraphFocusProvider — console redesign (docs/PHASE5_CONSOLE_REDESIGN_PLAN.md
 * §3, §4). Two small pieces of UI state that `CityGraph` (the graph itself),
 * `GraphPanel` (the maximise control), and `SectorHealthStrip` (a sibling
 * panel, not a descendant of either) all need to agree on:
 *
 *   `expanded`      — false: default sector-aggregated view (~11 nodes).
 *                      true: the maximise (`⤢`) control has grown the graph
 *                      to the full window showing all 50 curated assets
 *                      (D-R2 "expanded" view).
 *   `focusedSector` — in the default (non-expanded) view, which one sector
 *                      (if any) has been expanded inline so its real member
 *                      assets are shown instead of a single aggregate node.
 *                      Set by clicking a sector node in the graph OR a chip
 *                      in the sector health strip — both must drive the
 *                      same graph, so this can't live as local state in
 *                      either component (mirrors why `ConnectionProvider`/
 *                      `StreamProvider` are lifted to context rather than
 *                      each panel owning its own copy).
 *
 * Deliberately NOT part of `StreamProvider`/`ConnectionProvider`: this is
 * pure client-side view state, not a connection to an external system, so
 * it doesn't need the fetch/poll/reconnect machinery those contexts carry.
 */
interface GraphFocusValue {
  expanded: boolean;
  setExpanded: (value: boolean | ((prev: boolean) => boolean)) => void;
  focusedSector: string | null;
  setFocusedSector: (value: string | null | ((prev: string | null) => string | null)) => void;
}

const GraphFocusContext = createContext<GraphFocusValue | null>(null);

export function GraphFocusProvider({ children }: { children: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const [focusedSector, setFocusedSector] = useState<string | null>(null);

  return (
    <GraphFocusContext.Provider value={{ expanded, setExpanded, focusedSector, setFocusedSector }}>
      {children}
    </GraphFocusContext.Provider>
  );
}

/** Read/write the shared graph focus state. Must be called under `GraphFocusProvider`. */
export function useGraphFocus(): GraphFocusValue {
  const ctx = useContext(GraphFocusContext);
  if (!ctx) {
    throw new Error("useGraphFocus must be used within a GraphFocusProvider");
  }
  return ctx;
}
