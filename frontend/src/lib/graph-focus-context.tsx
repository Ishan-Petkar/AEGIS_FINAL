"use client";

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

/**
 * GraphFocusProvider — console redesign (docs/PHASE5_CONSOLE_REDESIGN_PLAN.md
 * §3, §4). Two small pieces of UI state that `CityGraph` (the graph itself),
 * `GraphPanel` (the maximise control), and `SectorHealthStrip` (a sibling
 * panel, not a descendant of either) all need to agree on:
 *
 *   `expanded`       — false: default sector-aggregated view (~11 nodes).
 *                       true: the maximise (`⤢`) control has grown the graph
 *                       to the full window showing all 50 curated assets
 *                       (D-R2 "expanded" view).
 *   `focusedSectors` — in the default (non-expanded) view, which sectors (if
 *                       any) have been expanded inline so their real member
 *                       assets are shown instead of a single aggregate node.
 *                       A SET, not a single value: focusing a sector is
 *                       stackable — clicking a second sector chip/node adds
 *                       it alongside whatever is already focused rather than
 *                       replacing it, so an operator can compare two or more
 *                       sectors side by side without losing the first one.
 *                       Toggle membership via `toggleFocusedSector`; clear
 *                       everything via `clearFocusedSectors` (Escape, or
 *                       clicking the hub while anything is focused). Set by
 *                       clicking a sector node in the graph OR a chip in the
 *                       sector health strip — both must drive the same
 *                       graph, so this can't live as local state in either
 *                       component (mirrors why `ConnectionProvider`/
 *                       `StreamProvider` are lifted to context rather than
 *                       each panel owning its own copy).
 *
 * Deliberately NOT part of `StreamProvider`/`ConnectionProvider`: this is
 * pure client-side view state, not a connection to an external system, so
 * it doesn't need the fetch/poll/reconnect machinery those contexts carry.
 */
interface GraphFocusValue {
  expanded: boolean;
  setExpanded: (value: boolean | ((prev: boolean) => boolean)) => void;
  focusedSectors: ReadonlySet<string>;
  /** Adds `sector` if it isn't already focused, removes it if it is — the
   * stackable toggle every click site (graph node, health-strip chip)
   * should call, rather than reaching for a raw setter that could
   * regress back to single-select-by-accident. */
  toggleFocusedSector: (sector: string) => void;
  /** Idempotent "make sure this sector is focused" — adds it if absent,
   * leaves the set unchanged (in particular, does NOT unfocus it) if
   * already present. For automatic/programmatic focusing (e.g. a cascade
   * animation bringing its origin sector into view) where the intent is
   * "ensure visible", never "toggle" — a toggle here would incorrectly
   * unfocus a sector an operator had already clicked on manually. */
  focusSector: (sector: string) => void;
  /** Unfocuses every sector at once (Escape, clicking the hub). */
  clearFocusedSectors: () => void;
}

const GraphFocusContext = createContext<GraphFocusValue | null>(null);

export function GraphFocusProvider({ children }: { children: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const [focusedSectors, setFocusedSectors] = useState<ReadonlySet<string>>(new Set());

  const toggleFocusedSector = useCallback((sector: string) => {
    setFocusedSectors((prev) => {
      const next = new Set(prev);
      if (next.has(sector)) {
        next.delete(sector);
      } else {
        next.add(sector);
      }
      return next;
    });
  }, []);

  const focusSector = useCallback((sector: string) => {
    setFocusedSectors((prev) => (prev.has(sector) ? prev : new Set(prev).add(sector)));
  }, []);

  const clearFocusedSectors = useCallback(() => {
    setFocusedSectors((prev) => (prev.size === 0 ? prev : new Set()));
  }, []);

  return (
    <GraphFocusContext.Provider
      value={{ expanded, setExpanded, focusedSectors, toggleFocusedSector, focusSector, clearFocusedSectors }}
    >
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
