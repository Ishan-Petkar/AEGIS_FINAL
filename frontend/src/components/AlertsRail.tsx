"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "./Panel";
import { SeverityGlyph, type Severity } from "./SeverityGlyph";
import { ApiError, ApiNetworkError, ackAlert, getAlerts, getCii } from "@/lib/api";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";
import type { AlertEnvelopeData, AlertOut, CiiResponse } from "@/lib/types";

// Bounded REST history load (D15-1) — "recent alerts", not "all alerts
// ever". Well above the live WS buffer's cap (50) so a freshly-loaded
// console shows real history, not just whatever has streamed in since.
const ALERTS_HISTORY_LIMIT = 100;

// ---------------------------------------------------------------------------
// Merged alert record — REST (`AlertOut`) and live (`AlertEnvelopeData`)
// alerts are shaped almost identically; this is the union the UI renders,
// with `acknowledged_at` optional since the WS envelope never carries it
// (Ticket #9's `_broadcast_batch` payload is deliberately smaller than the
// DB row — see `types.ts`).
// ---------------------------------------------------------------------------

interface AlertRecord {
  id: number;
  ts: string;
  severity: string;
  asset: string;
  title: string;
  detail: string | null;
  explanation: Record<string, unknown> | null;
  cii_snapshot_id: number | null;
  acknowledged: boolean;
  acknowledged_at: string | null;
}

function fromAlertOut(a: AlertOut): AlertRecord {
  return { ...a, ts: typeof a.ts === "string" ? a.ts : String(a.ts) };
}

function fromEnvelope(a: AlertEnvelopeData): AlertRecord {
  return {
    id: a.id,
    ts: a.ts,
    severity: a.severity,
    asset: a.asset,
    title: a.title,
    detail: a.detail,
    explanation: a.explanation,
    cii_snapshot_id: a.cii_snapshot_id,
    acknowledged: a.acknowledged,
    acknowledged_at: null,
  };
}

/** Dedupe-by-id merge (D15-1): adds `incoming` records into `map` for any
 * id not already present, mutating `map` in place. First source wins on
 * a conflict — callers merge the REST history first, then the live
 * stream, so the durable Postgres copy takes precedence over a
 * re-broadcast of the same alert and an id is never rendered twice. */
function mergeInto(map: Map<number, AlertRecord>, incoming: AlertRecord[]): void {
  for (const record of incoming) {
    if (!map.has(record.id)) {
      map.set(record.id, record);
    }
  }
}

function sortNewestFirst(records: AlertRecord[]): AlertRecord[] {
  return [...records].sort((a, b) => {
    const ta = Date.parse(a.ts);
    const tb = Date.parse(b.ts);
    if (ta !== tb) return tb - ta;
    return b.id - a.id;
  });
}

const SEVERITY_BORDER: Record<Severity, string> = {
  critical: "border-l-sev-critical",
  warning: "border-l-sev-warning",
  normal: "border-l-sev-normal",
  info: "border-l-sev-info",
};

const SEVERITY_TEXT: Record<Severity, string> = {
  critical: "text-sev-critical",
  warning: "text-sev-warning",
  normal: "text-sev-normal",
  info: "text-sev-info",
};

function toSeverity(raw: string): Severity {
  if (raw === "critical" || raw === "warning" || raw === "normal" || raw === "info") {
    return raw;
  }
  // Backend today only emits "critical" | "warning" (backend/ingest.py),
  // but severity is a plain `str` on the wire — fall back rather than
  // crash on an unrecognized value instead of pretending it's "normal".
  return "info";
}

// ---------------------------------------------------------------------------
// The "why" — D15-2. `StreamingScorer.explain()` (backend/streaming.py)
// always returns every feature, sorted by |z| descending with degenerate
// ones last, plus a `summary` string that is ALREADY the correctly-worded
// payoff line (it either reads "<feature> N.N sigma <direction> baseline"
// or, for a degenerate baseline, "<feature> <value> vs constant warmup
// baseline ... sigma undefined" — never a fabricated sigma). We render
// that string verbatim as the lead line rather than reformatting it, so
// there is exactly one place (the backend) that decides when "sigma" is
// allowed to appear.
// ---------------------------------------------------------------------------

interface ExplanationFeature {
  name: string;
  value: number;
  baseline_mean: number;
  z: number | null;
  direction: string;
  degenerate_baseline: boolean;
}

interface Explanation {
  top_feature: string;
  summary: string;
  features: ExplanationFeature[];
}

function parseExplanation(raw: Record<string, unknown> | null): Explanation | null {
  if (!raw) return null;
  if (typeof raw.summary !== "string" || typeof raw.top_feature !== "string") return null;
  if (!Array.isArray(raw.features)) return null;
  const features: ExplanationFeature[] = [];
  for (const f of raw.features) {
    if (typeof f !== "object" || f === null) continue;
    const rec = f as Record<string, unknown>;
    if (typeof rec.name !== "string") continue;
    features.push({
      name: rec.name,
      value: typeof rec.value === "number" ? rec.value : NaN,
      baseline_mean: typeof rec.baseline_mean === "number" ? rec.baseline_mean : NaN,
      // Explicitly preserve null — a missing z is not a zero deviation.
      z: typeof rec.z === "number" ? rec.z : null,
      direction: typeof rec.direction === "string" ? rec.direction : "at",
      degenerate_baseline: rec.degenerate_baseline === true,
    });
  }
  return { top_feature: raw.top_feature, summary: raw.summary, features };
}

function FeatureLine({ f }: { f: ExplanationFeature }) {
  if (f.degenerate_baseline || f.z === null) {
    return (
      <li className="flex items-baseline justify-between gap-2">
        <span>{f.name}</span>
        <span className="text-text-mute" title="No variance in warmup — sigma undefined, not zero">
          {Number.isFinite(f.value) ? f.value.toLocaleString() : "—"} raw · baseline degenerate
        </span>
      </li>
    );
  }
  return (
    <li className="flex items-baseline justify-between gap-2">
      <span>{f.name}</span>
      <span>
        {Math.abs(f.z).toFixed(1)}σ {f.direction}
      </span>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Blast radius — fetched on expand, not eagerly (many alerts at demo
// speeds). `cii_snapshot_id === null` means the asset was never a node in
// the dependency graph (K8) — say so plainly rather than fetching or
// showing an empty list that reads as "no impact".
// ---------------------------------------------------------------------------

type CiiState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "loaded"; data: CiiResponse }
  | { kind: "not-in-graph" }
  | { kind: "error"; message: string };

function BlastRadius({ alert }: { alert: AlertRecord }) {
  const [state, setState] = useState<CiiState>({ kind: "idle" });

  function load() {
    if (state.kind === "loading" || state.kind === "loaded") return;
    if (alert.cii_snapshot_id === null) {
      setState({ kind: "not-in-graph" });
      return;
    }
    setState({ kind: "loading" });
    getCii(alert.asset)
      .then((data) => setState({ kind: "loaded", data }))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          setState({ kind: "not-in-graph" });
          return;
        }
        const message =
          err instanceof ApiNetworkError
            ? "Could not reach the backend for blast radius."
            : err instanceof ApiError
              ? `Blast radius request failed (HTTP ${err.status}): ${err.message}`
              : "Unknown error loading blast radius.";
        setState({ kind: "error", message });
      });
  }

  return (
    <details className="mt-2 group" onToggle={(e) => e.currentTarget.open && load()}>
      <summary className="cursor-pointer text-[11px] uppercase tracking-[0.06em] text-text-dim transition-colors duration-150 ease-out hover:text-text [&::-webkit-details-marker]:hidden">
        Blast radius
        {state.kind === "loaded" ? ` (${state.data.impacted_assets.length})` : ""}
      </summary>
      <div className="mt-1 pl-3 font-mono text-xs text-text-dim">
        {state.kind === "idle" && null}
        {state.kind === "loading" && <p>Computing…</p>}
        {state.kind === "not-in-graph" && (
          <p className="text-text-mute">
            No blast radius computed — asset not in the dependency graph.
          </p>
        )}
        {state.kind === "error" && <p className="text-sev-critical">{state.message}</p>}
        {state.kind === "loaded" && state.data.impacted_assets.length === 0 && (
          <p className="text-text-mute">CII computed — zero downstream assets impacted.</p>
        )}
        {state.kind === "loaded" && state.data.impacted_assets.length > 0 && (
          <ul className="flex flex-col gap-0.5">
            {state.data.impacted_assets.map((asset) => (
              <li key={asset}>{asset}</li>
            ))}
          </ul>
        )}
        {state.kind === "loaded" && (
          <p className="mt-1 text-text-mute">
            CII median {state.data.cii_median.toFixed(2)} (p5 {state.data.cii_p5.toFixed(2)} ·
            p95 {state.data.cii_p95.toFixed(2)})
          </p>
        )}
      </div>
    </details>
  );
}

// ---------------------------------------------------------------------------
// Ack flow — D15-3. Optimistic, with rollback + surfaced error on failure.
// ---------------------------------------------------------------------------

interface AckMeta {
  pending: boolean;
  error: string | null;
}

function AlertCard({
  alert,
  ackMeta,
  onAck,
}: {
  alert: AlertRecord;
  ackMeta: AckMeta | undefined;
  onAck: (id: number) => void;
}) {
  const severity = toSeverity(alert.severity);
  const explanation = parseExplanation(alert.explanation);
  const leadingFeatures = explanation?.features.slice(0, 3) ?? [];
  const restFeatures = explanation?.features.slice(3) ?? [];
  const pending = ackMeta?.pending ?? false;

  return (
    <article
      className={`glass-panel border-l-2 p-3 ${SEVERITY_BORDER[severity]} ${
        alert.acknowledged ? "bg-glass-raised !border-l-glass-border-strong" : ""
      }`}
    >
      <div className="flex items-center gap-2">
        <SeverityGlyph severity={severity} />
        <span
          className={`text-[10px] font-semibold uppercase tracking-[0.08em] ${SEVERITY_TEXT[severity]}`}
        >
          {alert.severity}
        </span>
        <span className="ml-auto font-mono text-xs text-text-mute">{alert.asset}</span>
      </div>

      <p className="mt-2 text-xs font-semibold uppercase tracking-[0.04em] text-text-dim">
        {alert.title}
      </p>

      {explanation ? (
        <p className="mt-1 text-sm text-text">{explanation.summary}</p>
      ) : (
        <p className="mt-1 text-sm text-text-mute">
          {alert.detail ?? "No explanation available for this alert."}
        </p>
      )}

      {explanation && leadingFeatures.length > 0 && (
        <details className="mt-2 group">
          <summary className="cursor-pointer text-[11px] uppercase tracking-[0.06em] text-text-dim transition-colors duration-150 ease-out hover:text-text [&::-webkit-details-marker]:hidden">
            Feature deviations ({explanation.features.length})
          </summary>
          <ul className="mt-1 flex flex-col gap-0.5 pl-3 font-mono text-xs text-text-dim">
            {leadingFeatures.map((f) => (
              <FeatureLine key={f.name} f={f} />
            ))}
            {restFeatures.map((f) => (
              <FeatureLine key={f.name} f={f} />
            ))}
          </ul>
        </details>
      )}

      <BlastRadius alert={alert} />

      {ackMeta?.error && (
        <p className="mt-2 text-xs text-sev-critical" role="alert">
          {ackMeta.error}
        </p>
      )}

      <div className="mt-3 flex items-center justify-end gap-2">
        {alert.acknowledged && alert.acknowledged_at && (
          <span className="font-mono text-[10px] text-text-mute">
            acked {new Date(alert.acknowledged_at).toLocaleTimeString()}
          </span>
        )}
        <button
          type="button"
          disabled={alert.acknowledged || pending}
          onClick={() => onAck(alert.id)}
          aria-label={
            alert.acknowledged
              ? `${alert.asset} alert already acknowledged`
              : `Acknowledge ${alert.asset} alert`
          }
          className="rounded-[var(--radius-dense)] border border-glass-border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim transition-colors duration-150 ease-out hover:bg-glass-raised hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
        >
          {alert.acknowledged ? "Acked" : pending ? "Acking…" : "Ack"}
        </button>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------------------
// AlertsRail — merges REST history with the live stream (D15-1), never
// calling `useEventStream()` directly (the duplicate-socket defect fixed
// in Ticket #10) — only the shared `useStream()` context.
// ---------------------------------------------------------------------------

type HistoryState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "loaded" };

export function AlertsRail() {
  const { alerts: liveAlerts } = useStream();
  const { reconnectEpoch } = useConnection();
  const [history, setHistory] = useState<HistoryState>({ kind: "loading" });
  const [restAlerts, setRestAlerts] = useState<AlertRecord[]>([]);
  // Ack outcomes are user-driven (never derived from another render's
  // state), so they live in their own map keyed by id and are applied on
  // top of the REST/live merge below — this is what lets ACK roll back
  // without fighting the merge for authority over `acknowledged`.
  const [ackOverrides, setAckOverrides] = useState<
    Map<number, { acknowledged: boolean; acknowledged_at: string | null }>
  >(new Map());
  const [ackMetaById, setAckMetaById] = useState<Map<number, AckMeta>>(new Map());
  const [retryToken, setRetryToken] = useState(0);

  // D15-1: load REST history on mount, and again whenever the shared
  // connection transitions back to reachable (same pattern as
  // GraphPanel) or on manual retry.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      setHistory({ kind: "loading" });
      try {
        const resp = await getAlerts({ limit: ALERTS_HISTORY_LIMIT });
        if (cancelled) return;
        setRestAlerts(resp.alerts.map(fromAlertOut));
        setHistory({ kind: "loaded" });
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiNetworkError
            ? "Could not reach the backend for alert history."
            : err instanceof ApiError
              ? `Alert history request failed (HTTP ${err.status}): ${err.message}`
              : "Unknown error loading alert history.";
        setHistory({ kind: "error", message });
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [reconnectEpoch, retryToken]);

  // D15-1: merge REST history with the live stream, deduped by id
  // (`mergeInto` favors the REST copy when both exist, since it is the
  // durable Postgres record; a live envelope only wins when its id is
  // not present yet). This is a pure derivation over already-owned state
  // (restAlerts, liveAlerts, ackOverrides) computed at render time, not
  // an effect — there is no external system to synchronize here.
  const sorted = useMemo(() => {
    const map = new Map<number, AlertRecord>();
    mergeInto(map, restAlerts);
    mergeInto(map, liveAlerts.map(fromEnvelope));
    for (const [id, override] of ackOverrides) {
      const existing = map.get(id);
      if (existing) {
        map.set(id, {
          ...existing,
          acknowledged: override.acknowledged,
          acknowledged_at: override.acknowledged_at,
        });
      }
    }
    return sortNewestFirst(Array.from(map.values()));
  }, [restAlerts, liveAlerts, ackOverrides]);

  async function handleAck(id: number) {
    const prior = sorted.find((a) => a.id === id);
    if (!prior || prior.acknowledged) return;

    setAckMetaById((prev) => new Map(prev).set(id, { pending: true, error: null }));
    setAckOverrides((prev) =>
      new Map(prev).set(id, { acknowledged: true, acknowledged_at: prior.acknowledged_at })
    );

    try {
      const updated = await ackAlert(id);
      setAckOverrides((prev) =>
        new Map(prev).set(id, {
          acknowledged: updated.acknowledged,
          acknowledged_at: updated.acknowledged_at,
        })
      );
      setAckMetaById((prev) => new Map(prev).set(id, { pending: false, error: null }));
    } catch (err) {
      // Roll back — a false "acknowledged" record is worse than a slow UI.
      setAckOverrides((prev) => {
        const next = new Map(prev);
        next.delete(id);
        return next;
      });
      const message =
        err instanceof ApiNetworkError
          ? "Could not reach the backend — acknowledgement was not saved."
          : err instanceof ApiError
            ? `Acknowledge failed (HTTP ${err.status}): ${err.message}`
            : "Unknown error acknowledging alert.";
      setAckMetaById((prev) => new Map(prev).set(id, { pending: false, error: message }));
    }
  }

  const showHistoryError = history.kind === "error" && sorted.length === 0;

  // Console redesign (D-R3): real severity counts from the merged
  // REST+live alert list — never a fabricated summary. `critical`/
  // `warning` count only UN-acknowledged alerts (an acked critical isn't
  // still demanding attention); `acked` counts every acknowledged alert
  // regardless of severity, matching the plan's own example format
  // ("3 critical · 1 warning · 12 acked").
  let criticalCount = 0;
  let warningCount = 0;
  let ackedCount = 0;
  for (const a of sorted) {
    if (a.acknowledged) {
      ackedCount += 1;
      continue;
    }
    const severity = toSeverity(a.severity);
    if (severity === "critical") criticalCount += 1;
    else if (severity === "warning") warningCount += 1;
  }

  return (
    <Panel
      label="Active Alerts"
      // Console redesign (D-R1): narrowed from 380px to 340px — the graph
      // (the hero region) gets the width back.
      className="w-full lg:w-[340px] lg:shrink-0"
      bodyClassName="flex flex-col gap-3 overflow-y-auto"
      action={
        sorted.length > 0 ? (
          <span className="font-mono text-[10px] normal-case tracking-normal text-text-dim" aria-live="polite">
            {criticalCount} critical &middot; {warningCount} warning &middot; {ackedCount} acked
          </span>
        ) : undefined
      }
    >
      {history.kind === "loading" && sorted.length === 0 && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-text-dim">
          <span
            className="h-6 w-6 animate-spin rounded-full border-2 border-glass-border-strong border-t-accent"
            aria-hidden="true"
          />
          <p className="text-sm">Loading alerts…</p>
        </div>
      )}

      {showHistoryError && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center">
          <p className="text-sm font-medium text-sev-critical">Alerts unavailable</p>
          <p className="font-mono text-xs text-text-mute">
            {history.kind === "error" ? history.message : ""}
          </p>
          <button
            type="button"
            onClick={() => setRetryToken((n) => n + 1)}
            className="mt-1 rounded-[var(--radius-dense)] border border-glass-border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim transition-colors duration-150 ease-out hover:bg-glass-raised hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            Retry
          </button>
        </div>
      )}

      {history.kind !== "loading" && !showHistoryError && sorted.length === 0 && (
        <div className="flex flex-1 flex-col items-center justify-center gap-1 text-center">
          <p className="text-sm font-medium text-text-dim">No active alerts</p>
          <p className="font-mono text-xs text-text-mute">
            Nothing anomalous detected yet this session.
          </p>
        </div>
      )}

      {sorted.map((alert) => (
        <AlertCard
          key={alert.id}
          alert={alert}
          ackMeta={ackMetaById.get(alert.id)}
          onAck={handleAck}
        />
      ))}
    </Panel>
  );
}
