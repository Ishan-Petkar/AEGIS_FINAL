"use client";

/**
 * src/lib/theme-tokens.ts — resolves DESIGN_CONSOLE.md §2 CSS custom
 * properties (defined once in `globals.css`) to literal color strings at
 * runtime, for the one legitimate exception to "no raw hex in
 * components": `<canvas>` 2D contexts cannot consume `var(--token)`
 * (canvas painting isn't part of the CSS cascade), so `CityGraph.tsx`'s
 * force-directed graph needs actual resolved values to hand to
 * `ctx.fillStyle` / `ctx.strokeStyle`. Reading them via
 * `getComputedStyle` — rather than hardcoding a parallel hex palette in
 * this file — keeps `globals.css` the single source of truth; nothing
 * here duplicates a color value, it only reads the one that already
 * exists. Fallbacks are CSS keyword colors (never hex/rgba), used only
 * before the effect below has run (there is no visible flash — canvas
 * painting only happens client-side, after mount).
 */

import { useEffect, useState } from "react";

function isBrowser(): boolean {
  return typeof document !== "undefined";
}

export interface ThemeColors {
  ground: string;
  groundRaised: string;
  glassBorder: string;
  glassBorderStrong: string;
  text: string;
  textDim: string;
  textMute: string;
  accent: string;
  accentHi: string;
  sevCritical: string;
  sevWarning: string;
  sevNormal: string;
  sevInfo: string;
  financial: string;
}

const FALLBACK: ThemeColors = {
  ground: "whitesmoke",
  groundRaised: "white",
  glassBorder: "lightgray",
  glassBorderStrong: "darkgray",
  text: "#101828",
  textDim: "#475467",
  textMute: "#667085",
  accent: "royalblue",
  accentHi: "cornflowerblue",
  sevCritical: "crimson",
  sevWarning: "darkorange",
  sevNormal: "seagreen",
  sevInfo: "steelblue",
  financial: "rebeccapurple",
};

const VAR_NAMES: Record<keyof ThemeColors, string> = {
  ground: "--ground",
  groundRaised: "--ground-raised",
  glassBorder: "--glass-border",
  glassBorderStrong: "--glass-border-strong",
  text: "--text",
  textDim: "--text-dim",
  textMute: "--text-mute",
  accent: "--accent",
  accentHi: "--accent-hi",
  sevCritical: "--sev-critical",
  sevWarning: "--sev-warning",
  sevNormal: "--sev-normal",
  sevInfo: "--sev-info",
  financial: "--financial",
};

function resolveThemeColors(): ThemeColors {
  if (!isBrowser()) return FALLBACK;
  const style = getComputedStyle(document.documentElement);
  const read = (name: string, fallback: string) => {
    const v = style.getPropertyValue(name).trim();
    return v.length > 0 ? v : fallback;
  };
  const resolved = {} as ThemeColors;
  (Object.keys(VAR_NAMES) as (keyof ThemeColors)[]).forEach((key) => {
    resolved[key] = read(VAR_NAMES[key], FALLBACK[key]);
  });
  return resolved;
}

/**
 * Reads the resolved DESIGN_CONSOLE.md palette, for canvas use. Resolved
 * lazily in `useState`'s initializer (this hook is only ever called from
 * a client-only component — `CityGraph` is loaded via `next/dynamic`
 * with `ssr: false` — so `document` is always available by first render)
 * rather than in an effect, which would cause an extra render just to
 * replace the fallback.
 */
export function useThemeColors(): ThemeColors {
  const [colors] = useState<ThemeColors>(resolveThemeColors);
  return colors;
}

/**
 * Resolves the `--font-mono` token to an actual font-family list via a
 * detached probe element, since a canvas `font` string can't reference a
 * CSS custom property either. Falls back to the generic `monospace`
 * keyword (not a literal face name) until the effect runs.
 */
function resolveMonoFontFamily(): string {
  if (!isBrowser()) return "monospace";
  const probe = document.createElement("span");
  probe.className = "font-mono";
  probe.style.position = "absolute";
  probe.style.visibility = "hidden";
  probe.style.pointerEvents = "none";
  document.body.appendChild(probe);
  const resolved = getComputedStyle(probe).fontFamily;
  document.body.removeChild(probe);
  return resolved || "monospace";
}

export function useMonoFontFamily(): string {
  const [family] = useState(resolveMonoFontFamily);
  return family;
}

/** DESIGN_CONSOLE.md §4/§7: pulses and cascade animation must respect this. */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => isBrowser() && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handler = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  return reduced;
}
