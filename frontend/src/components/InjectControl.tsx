"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  ApiNetworkError,
  getInjectScenarios,
  injectScenario,
} from "@/lib/api";
import { useTopology } from "@/lib/topology-context";
import type { ScenarioOut } from "@/lib/types";

const DEFAULT_TARGET_ASSET = "City_Payment_Gateway";
const DEFAULT_COUNT = 100;

// No "loading" variant: the fetch effect below never calls `setState`
// synchronously in its own body (only from inside the `.then`/`.catch`
// callbacks — see that effect's comment), so "loading" is rendered as a
// derived condition (`open && kind === "idle"`) instead of a stored state.
type ScenariosState =
  | { kind: "idle" }
  | { kind: "loaded"; scenarios: ScenarioOut[] }
  | { kind: "error"; message: string };

type SubmitState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "success"; message: string }
  | { kind: "error"; message: string };

/**
 * InjectControl (Phase A improvement pass, roadmap item "Wire the Speed and
 * Inject controls") — replaces the permanently-disabled Inject button
 * (formerly "Ticket #13" placeholder) with a real popover that calls
 * `GET /api/inject/scenarios` and `POST /api/inject`.
 *
 * Every scenario replays REAL captured CIC-IDS2017 attack flows re-targeted
 * at an operator-chosen curated asset (never fabricated — see
 * `backend/inject.py`'s module docstring) so this is deliberately not a
 * free-text form: scenario names come only from the backend's own registry,
 * and target assets come only from the live topology, filtered to curated
 * assets with a real static IP (`is_gateway === false && type !==
 * "synthesized"`, mirroring `backend.inject.resolvable_target_assets()`'s
 * stricter-than-plain-graph-membership gate) — a gateway or synthesized node
 * is never offered, since injecting against one would silently resolve to
 * some other asset on the backend rather than the one the operator picked.
 */
export function InjectControl({ running }: { running: boolean }) {
  const [open, setOpen] = useState(false);
  const [scenariosState, setScenariosState] = useState<ScenariosState>({ kind: "idle" });
  const [scenario, setScenario] = useState<string>("");
  const [targetAsset, setTargetAsset] = useState<string>(DEFAULT_TARGET_ASSET);
  const [count, setCount] = useState<number>(DEFAULT_COUNT);
  const [submit, setSubmit] = useState<SubmitState>({ kind: "idle" });

  const { state: topologyState } = useTopology();

  const targetOptions = useMemo(() => {
    if (topologyState.kind !== "loaded") return [];
    return topologyState.data.nodes
      .filter((n) => !n.is_gateway && n.type !== "synthesized")
      .map((n) => n.name)
      .sort((a, b) => a.localeCompare(b));
  }, [topologyState]);

  // Fetch the scenario registry once, the first time the popover opens —
  // no point loading it before an operator has expressed interest.
  // `fetchingRef` (a ref, not state) guards against a duplicate in-flight
  // request; every `setState` call below happens inside the `.then`/
  // `.catch` callbacks, never synchronously in the effect body itself.
  const fetchingRef = useRef(false);
  useEffect(() => {
    if (!open || scenariosState.kind !== "idle" || fetchingRef.current) return;
    let cancelled = false;
    fetchingRef.current = true;
    getInjectScenarios()
      .then((res) => {
        if (cancelled) return;
        setScenariosState({ kind: "loaded", scenarios: res.scenarios });
        if (res.scenarios.length > 0) setScenario((prev) => prev || res.scenarios[0].name);
      })
      .catch((err) => {
        if (cancelled) return;
        const message =
          err instanceof ApiNetworkError
            ? "Could not reach the backend for the scenario list."
            : err instanceof ApiError
              ? `Scenario list failed (HTTP ${err.status}): ${err.message}`
              : "Unknown error loading scenarios.";
        setScenariosState({ kind: "error", message });
      })
      .finally(() => {
        fetchingRef.current = false;
      });
    return () => {
      cancelled = true;
    };
  }, [open, scenariosState.kind]);

  // Default the target-asset select to City_Payment_Gateway once the
  // topology has actually loaded and confirms that name exists — falls
  // back to the first available curated asset otherwise, never to a name
  // the backend would 422 on. Computed and corrected during render
  // (React's documented "adjusting state when a prop changes" pattern,
  // not an effect): the condition is idempotent — once `targetAsset` is a
  // member of `targetOptions` the branch stops firing, so this can never
  // loop or fight an operator's own valid selection.
  if (targetOptions.length > 0 && !targetOptions.includes(targetAsset)) {
    setTargetAsset(targetOptions.includes(DEFAULT_TARGET_ASSET) ? DEFAULT_TARGET_ASSET : targetOptions[0]);
  }

  async function handleSubmit() {
    if (!scenario) return;
    setSubmit({ kind: "pending" });
    try {
      const res = await injectScenario({ scenario, target_asset: targetAsset, count });
      setSubmit({
        kind: "success",
        message: `${res.flows_injected} real ${res.real_label} flows injected onto ${res.target_asset}.`,
      });
    } catch (err) {
      const message =
        err instanceof ApiNetworkError
          ? "Could not reach the backend — nothing was injected."
          : err instanceof ApiError
            ? err.status === 409
              ? "No replay is running — start replay before injecting (POST /api/replay/start)."
              : err.status === 401
                ? "Unauthorized — this backend requires an API token (set NEXT_PUBLIC_API_TOKEN)."
                : err.status === 429
                  ? "Rate limited — too many requests in a short window. Wait a moment and try again."
                  : `Inject failed (HTTP ${err.status}): ${err.message}`
            : "Unknown error injecting scenario.";
      setSubmit({ kind: "error", message });
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="Inject a what-if attack scenario"
        title={
          running
            ? "Inject a real, re-targeted attack scenario onto a curated asset"
            : "Replay is not running — inject will fail until one is started"
        }
        className="rounded-[var(--radius-panel)] border border-glass-border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-text transition-colors hover:border-accent hover:text-accent"
      >
        Inject
      </button>

      {/*
        The popover is deliberately NOT `.glass-panel`. It floats over the
        Active Alerts column, and `.glass-panel`'s 4.5%-opacity `--glass` fill
        is only legible thanks to the `backdrop-filter: blur(14px)` that is
        meant to accompany it — a blur that does not survive the build.
        Tailwind v4's Lightning CSS pass collapses the rule's
        `backdrop-filter` / `-webkit-backdrop-filter` pair down to the
        `-webkit-` form alone, and Chromium supports only the UNPREFIXED
        property there (`CSS.supports('-webkit-backdrop-filter','blur(1px)')`
        returns false), so the computed value is `none`. globals.css's
        `@supports not (...)` opaque fallback cannot rescue it either: that
        condition tests for standard support, which IS present. Net effect is
        a ~4%-opaque panel with no blur, through which alert text reads
        straight through. Every other `.glass-panel` sits over the flat
        `--ground` page background where this is invisible; only this popover
        overlaps real content, so it takes an opaque surface rather than
        depending on an effect that never reaches the browser.
      */}
      {open && (
        <div
          role="dialog"
          aria-label="Inject what-if scenario"
          className="absolute right-0 top-[calc(100%+8px)] z-50 w-72 rounded-[var(--radius-panel)] border border-glass-border bg-ground-raised p-3 shadow-lg"
        >
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-text-dim">
              What-if injection
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close"
              className="text-text-mute hover:text-text"
            >
              ×
            </button>
          </div>

          {!running && (
            <p className="mb-2 rounded-[var(--radius-dense)] border border-sev-warning/40 bg-sev-warning/10 px-2 py-1 text-[11px] leading-snug text-sev-warning">
              Replay isn&apos;t running. Injecting will 409 until one starts.
            </p>
          )}

          {scenariosState.kind === "idle" && (
            <p className="text-[11px] text-text-mute">Loading scenarios…</p>
          )}
          {scenariosState.kind === "error" && (
            <p className="text-[11px] text-sev-critical">{scenariosState.message}</p>
          )}

          {scenariosState.kind === "loaded" && (
            <div className="flex flex-col gap-2">
              <label className="flex flex-col gap-1 text-[10px] uppercase tracking-[0.06em] text-text-dim">
                Scenario
                <select
                  value={scenario}
                  onChange={(e) => setScenario(e.target.value)}
                  className="rounded-[var(--radius-dense)] border border-glass-border bg-transparent px-2 py-1 font-mono text-xs normal-case tracking-normal text-text"
                >
                  {scenariosState.scenarios.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name} — {s.label} ({s.day})
                    </option>
                  ))}
                </select>
              </label>

              <label className="flex flex-col gap-1 text-[10px] uppercase tracking-[0.06em] text-text-dim">
                Target asset
                <select
                  value={targetAsset}
                  onChange={(e) => setTargetAsset(e.target.value)}
                  disabled={targetOptions.length === 0}
                  className="rounded-[var(--radius-dense)] border border-glass-border bg-transparent px-2 py-1 font-mono text-xs normal-case tracking-normal text-text disabled:opacity-50"
                >
                  {targetOptions.length === 0 && <option>Loading topology…</option>}
                  {targetOptions.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>

              <label className="flex flex-col gap-1 text-[10px] uppercase tracking-[0.06em] text-text-dim">
                Flow count
                <input
                  type="number"
                  min={1}
                  value={count}
                  onChange={(e) => setCount(Math.max(1, Number(e.target.value) || 1))}
                  className="rounded-[var(--radius-dense)] border border-glass-border bg-transparent px-2 py-1 font-mono text-xs normal-case tracking-normal text-text"
                />
              </label>

              <button
                type="button"
                onClick={handleSubmit}
                disabled={submit.kind === "pending" || !scenario}
                className="mt-1 rounded-[var(--radius-panel)] bg-accent px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ground disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submit.kind === "pending" ? "Injecting…" : "Inject flows"}
              </button>

              {submit.kind === "success" && (
                <p className="text-[11px] leading-snug text-sev-normal">{submit.message}</p>
              )}
              {submit.kind === "error" && (
                <p className="text-[11px] leading-snug text-sev-critical">{submit.message}</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
