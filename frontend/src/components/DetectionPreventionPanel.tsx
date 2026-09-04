"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { Panel } from "./Panel";
import { SeverityGlyph, type Severity } from "./SeverityGlyph";
import { ApiError, ApiNetworkError, getIpsActions, rollbackIpsAction } from "@/lib/api";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";
import type { EventEnvelopeData, IpsActionEnvelopeData } from "@/lib/types";

/** The shape both transports agree on. `IpsActionOut` (REST) additionally
 * carries `replay_session_id`, which nothing here renders, so the live
 * envelope's narrower type is the common denominator and both merge into
 * one list without a lossy adapter in between. */
type IpsActionRecord = IpsActionEnvelopeData;

interface RollbackMeta {
  pending: boolean;
  error: string | null;
}

// DetectionPreventionPanel — the detailed injections/detections/
// preventions view (console redesign, light-theme dashboard pass).
// Replaces the old narrow IpsActionsRail side-rail with a full-width,
// tabbed panel: PREVENTION (IPS) shows every approved prevention
// decision with a working rollback, DETECTION (IDS) shows the live
// feed's own anomalous/injected events. Every row on both tabs is real
// backend data — nothing here is a mock table.

const HISTORY_LIMIT = 100;
const ACTIVE_STATUSES = new Set(["simulated", "enforced"]);
const ACTIVE_PREVENTION_ACTIONS = new Set(["rate_limit", "block", "quarantine"]);

function isActivePrevention(a: { action: string; status: string; rolled_back_at: string | null }) {
  return ACTIVE_PREVENTION_ACTIONS.has(a.action) && ACTIVE_STATUSES.has(a.status) && !a.rolled_back_at;
}

function formatTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString([], { hour12: false });
}

const ACTION_LABEL: Record<string, string> = {
  quarantine: "Quarantine",
  block: "Block",
  rate_limit: "Rate-limit",
  alert: "Alert only",
  observe: "Observe",
};

const ACTION_TONE: Record<string, Severity> = {
  quarantine: "critical",
  block: "critical",
  rate_limit: "warning",
  alert: "info",
  observe: "normal",
};

function ActionBadge({ action }: { action: string }) {
  const tone = ACTION_TONE[action] ?? "info";
  const cls =
    tone === "critical"
      ? "bg-sev-critical/10 text-sev-critical"
      : tone === "warning"
        ? "bg-sev-warning/10 text-sev-warning"
        : tone === "normal"
          ? "bg-sev-normal/10 text-sev-normal"
          : "bg-sev-info/10 text-sev-info";
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${cls}`}>
      <SeverityGlyph severity={tone} />
      {ACTION_LABEL[action] ?? action}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Tab 1 — Prevention (IPS)
// ---------------------------------------------------------------------------

type LoadState = { kind: "loading" } | { kind: "error"; message: string } | { kind: "loaded" };

function friendlyError(err: unknown, fallback: string): string {
  if (err instanceof ApiNetworkError) return "Could not reach the backend.";
  if (err instanceof ApiError) return `Request failed (HTTP ${err.status}): ${err.message}`;
  return fallback;
}

/** Terminal states have `rolled_back_at` set or a non-active status. Used
 * as a merge rank: a record showing a MORE advanced lifecycle wins over
 * one showing a less advanced one, whichever transport it arrived on. */
function lifecycleRank(a: IpsActionRecord): number {
  return a.rolled_back_at || !ACTIVE_STATUSES.has(a.status) ? 1 : 0;
}

/**
 * Merge REST history with the live `ips_action` stream, deduped by id.
 *
 * Deliberately NOT "REST always wins" (the rule `AlertsRail` uses for
 * alerts): unlike an alert, one IPS action mutates over its lifetime —
 * created, then expired by TTL, then possibly rolled back — and each
 * transition is broadcast on the same id. Whichever copy shows the more
 * advanced lifecycle is the true one, regardless of how it arrived; a
 * REST snapshot fetched before a rollback must not overwrite the live
 * envelope announcing it, and vice-versa.
 */
function mergeActions(sources: IpsActionRecord[][]): IpsActionRecord[] {
  const map = new Map<number, IpsActionRecord>();
  for (const list of sources) {
    for (const rec of list) {
      const existing = map.get(rec.id);
      if (!existing || lifecycleRank(rec) > lifecycleRank(existing)) {
        map.set(rec.id, rec);
      }
    }
  }
  return [...map.values()].sort((a, b) => {
    const ta = Date.parse(a.ts);
    const tb = Date.parse(b.ts);
    if (ta !== tb) return tb - ta;
    return b.id - a.id;
  });
}

function PreventionTab() {
  const [restActions, setRestActions] = useState<IpsActionRecord[]>([]);
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [rollbackMetaById, setRollbackMetaById] = useState<Map<number, RollbackMeta>>(new Map());
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const [retryToken, setRetryToken] = useState(0);
  const { reconnectEpoch } = useConnection();
  const { ipsActions: liveActions } = useStream();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setState({ kind: "loading" });
      try {
        // TWO action queries, not one. The recent-history page is capped
        // at HISTORY_LIMIT, so an action that is still ACTIVE but older
        // than that window would be invisible — and "Active Mitigations"
        // would under-count exactly the long-lived mitigations an
        // operator most needs to know about. The server-side `active`
        // filter answers that question directly; both results merge into
        // the same map below, so an action appearing in both is one row.
        const [recentResp, activeResp] = await Promise.all([
          getIpsActions({ limit: HISTORY_LIMIT }),
          getIpsActions({ active: true, limit: HISTORY_LIMIT }),
        ]);
        if (cancelled) return;
        setRestActions(mergeActions([recentResp.actions, activeResp.actions]));
        setState({ kind: "loaded" });
      } catch (err) {
        if (cancelled) return;
        setState({ kind: "error", message: friendlyError(err, "Unknown error loading IPS data.") });
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [reconnectEpoch, retryToken]);

  const actions = useMemo(
    () => mergeActions([restActions, liveActions]),
    [restActions, liveActions]
  );
  const activeCount = useMemo(() => actions.filter(isActivePrevention).length, [actions]);

  function toggleExpanded(id: number) {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handleRollback(id: number) {
    setRollbackMetaById((prev) => new Map(prev).set(id, { pending: true, error: null }));
    try {
      await rollbackIpsAction(id);
      setRollbackMetaById((prev) => new Map(prev).set(id, { pending: false, error: null }));
      setRetryToken((n) => n + 1);
    } catch (err) {
      // A 409 is the one "failure" that is really just staleness — the
      // action already expired or was rolled back elsewhere — so it
      // refetches quietly. Everything else (404, 401, 429, network) is a
      // real failure an operator must see: silently swallowing it would
      // leave them believing a mitigation was lifted when it was not.
      const alreadyInactive = err instanceof ApiError && err.status === 409;
      setRollbackMetaById((prev) =>
        new Map(prev).set(id, {
          pending: false,
          error: alreadyInactive ? null : friendlyError(err, "Unknown error rolling back."),
        })
      );
      if (alreadyInactive) setRetryToken((n) => n + 1);
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="grid grid-cols-2 gap-3">
        <StatBlock label="Active Mitigations" value={String(activeCount)} />
        <StatBlock label="Recent Decisions" value={String(actions.length)} />
      </div>

      {state.kind === "loading" && actions.length === 0 && <LoadingRow label="Loading prevention decisions…" />}
      {state.kind === "error" && actions.length === 0 && (
        <ErrorRow message={state.message} onRetry={() => setRetryToken((n) => n + 1)} />
      )}
      {state.kind !== "loading" && actions.length === 0 && state.kind !== "error" && (
        <EmptyRow message="No prevention decisions yet — the IPS layer only acts once the Hybrid IDS layer has fused a corroborated signal." />
      )}

      {actions.length > 0 && (
        <div className="min-h-0 flex-1 overflow-auto rounded-[var(--radius-dense)] border border-glass-border">
          <table className="w-full min-w-[760px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-glass-border bg-glass-raised text-[10px] uppercase tracking-[0.06em] text-text-mute">
                <th className="px-3 py-2 font-semibold">Time</th>
                <th className="px-3 py-2 font-semibold">Target Asset</th>
                <th className="px-3 py-2 font-semibold">Action</th>
                <th className="px-3 py-2 font-semibold">Confidence</th>
                <th className="px-3 py-2 font-semibold" />
              </tr>
            </thead>
            <tbody>
              {actions.map((a) => {
                const active = isActivePrevention(a);
                const meta = rollbackMetaById.get(a.id);
                const pending = meta?.pending ?? false;
                const expanded = expandedIds.has(a.id);
                return (
                  <Fragment key={a.id}>
                    <tr
                      className="cursor-pointer border-b border-glass-border last:border-0 hover:bg-glass-raised"
                      onClick={() => toggleExpanded(a.id)}
                      aria-expanded={expanded}
                    >
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-text-mute">
                        <span className="mr-1 inline-block w-2 text-text-mute" aria-hidden="true">
                          {expanded ? "▾" : "▸"}
                        </span>
                        {formatTime(a.ts)}
                      </td>
                      <td className="max-w-[220px] truncate px-3 py-2 font-mono text-text" title={a.target_asset}>
                        {a.target_asset}
                      </td>
                      <td className="px-3 py-2">
                        <ActionBadge action={a.action} />
                        {a.dry_run && (
                          <span className="ml-1.5 rounded-full border border-glass-border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.05em] text-text-mute">
                            dry-run
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 font-mono tabular-nums text-text-dim">{a.confidence.toFixed(2)}</td>
                      <td className="px-3 py-2 text-right">
                        {active && (
                          <button
                            type="button"
                            disabled={pending}
                            onClick={(e) => {
                              e.stopPropagation();
                              handleRollback(a.id);
                            }}
                            className="rounded-[var(--radius-dense)] border border-glass-border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-text-dim transition-colors hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {pending ? "Rolling back…" : "Roll back"}
                          </button>
                        )}
                      </td>
                    </tr>

                    {meta?.error && (
                      <tr className="border-b border-glass-border last:border-0">
                        <td colSpan={5} className="px-3 pb-2 text-[11px] text-sev-critical" role="alert">
                          {meta.error}
                        </td>
                      </tr>
                    )}

                    {expanded && (
                      <tr className="border-b border-glass-border bg-glass-raised last:border-0">
                        <td colSpan={5} className="px-3 py-3">
                          <p className="text-[11px] leading-relaxed text-text-dim">{a.reason}</p>

                          {a.rolled_back_at && (
                            <p className="mt-2 font-mono text-[10px] text-text-mute">
                              {a.rollback_reason ?? "rolled back"} · {formatTime(a.rolled_back_at)}
                            </p>
                          )}

                          <EvidenceGrid evidence={a.evidence} />

                          {a.triggering_event_id !== null && (
                            <p className="mt-2 font-mono text-[10px] text-text-mute">
                              triggering event #{a.triggering_event_id}
                            </p>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/**
 * The decision's own evidence snapshot, exactly as the policy engine
 * recorded it (`backend/ips/policy.py`'s `_evidence`). Rendered from
 * whatever keys are actually present rather than a hardcoded list, so a
 * future field added backend-side shows up here without a frontend
 * change — and a missing one simply doesn't render, instead of printing
 * a fabricated zero.
 */
function EvidenceGrid({ evidence }: { evidence: Record<string, unknown> | null }) {
  if (!evidence) return null;
  const entries = Object.entries(evidence).filter(([, v]) => v !== null && v !== undefined);
  if (entries.length === 0) return null;
  return (
    <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
      {entries.map(([k, v]) => (
        <div key={k} className="flex min-w-0 flex-col">
          <dt className="text-[9px] uppercase tracking-[0.06em] text-text-mute">{k.replace(/_/g, " ")}</dt>
          <dd className="truncate font-mono text-[11px] text-text-dim" title={String(v)}>
            {Array.isArray(v) ? (v.length > 0 ? v.join(", ") : "—") : typeof v === "number" ? v.toFixed(3).replace(/\.?0+$/, "") : String(v)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// Tab 2 — Detection (IDS)
// ---------------------------------------------------------------------------

function isNotableEvent(e: EventEnvelopeData): boolean {
  return e.is_anomaly || e.tripwire_fired || (e.hybrid !== null && e.hybrid.action !== "observe");
}

function displayIdentity(asset: string, ip: string): string {
  if (!asset || asset.startsWith("Unresolved_")) return ip;
  return asset;
}

function eventSeverity(e: EventEnvelopeData): Severity {
  if (e.tripwire_fired) return "critical";
  if (e.hybrid?.band === "confirmed") return "critical";
  if (e.is_anomaly || e.hybrid?.band === "likely") return "warning";
  return "info";
}

function DetectionTab() {
  const { events } = useStream();
  const notable = useMemo(() => events.filter(isNotableEvent).slice(0, HISTORY_LIMIT), [events]);

  const tripwireCount = useMemo(() => events.filter((e) => e.tripwire_fired).length, [events]);
  const anomalyCount = useMemo(() => events.filter((e) => e.is_anomaly).length, [events]);
  const injectedCount = useMemo(() => events.filter((e) => e.batch_origin === "injected").length, [events]);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="grid grid-cols-3 gap-3">
        <StatBlock label="Tripwire Hits" value={String(tripwireCount)} valueClassName={tripwireCount > 0 ? "text-sev-critical" : undefined} />
        <StatBlock label="Anomalies (buffer)" value={String(anomalyCount)} />
        <StatBlock label="Injected Flows" value={String(injectedCount)} valueClassName={injectedCount > 0 ? "text-accent" : undefined} />
      </div>

      {notable.length === 0 ? (
        <EmptyRow message="No anomalous or injected traffic in the live buffer yet." />
      ) : (
        <div className="min-h-0 flex-1 overflow-auto rounded-[var(--radius-dense)] border border-glass-border">
          <table className="w-full min-w-[760px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-glass-border bg-glass-raised text-[10px] uppercase tracking-[0.06em] text-text-mute">
                <th className="px-3 py-2 font-semibold">Time</th>
                <th className="px-3 py-2 font-semibold">Source → Destination</th>
                <th className="px-3 py-2 font-semibold">Detector(s) Fired</th>
                <th className="px-3 py-2 font-semibold">Score</th>
                <th className="px-3 py-2 font-semibold">Severity</th>
              </tr>
            </thead>
            <tbody>
              {notable.map((e) => {
                const src = displayIdentity(e.source_asset, e.source_ip);
                const dst = displayIdentity(e.destination_asset, e.destination_ip);
                const severity = eventSeverity(e);
                const detectors: string[] = [];
                if (e.tripwire_fired) detectors.push("tripwire");
                if (e.is_anomaly) detectors.push("isolation_forest");
                if (e.hybrid) for (const d of e.hybrid.fired_detectors) if (!detectors.includes(d)) detectors.push(d);
                return (
                  <tr key={e.id} className="border-b border-glass-border/60 last:border-0 hover:bg-glass-raised">
                    <td className="whitespace-nowrap px-3 py-2 font-mono text-text-mute">{formatTime(e.ts)}</td>
                    <td className="max-w-[260px] truncate px-3 py-2 font-mono text-text" title={`${src} → ${dst}`}>
                      {src} <span aria-hidden="true">&rarr;</span> {dst}
                      {e.batch_origin === "injected" && (
                        <span className="ml-1.5 rounded-full border border-accent/40 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.05em] text-accent">
                          injected
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 font-mono text-text-dim">
                      {detectors.length > 0 ? detectors.join(", ") : "—"}
                    </td>
                    <td className="px-3 py-2 font-mono tabular-nums text-text-dim">
                      {e.hybrid ? e.hybrid.threat_score.toFixed(2) : e.calibrated_score.toFixed(2)}
                    </td>
                    <td className="px-3 py-2">
                      <span className="inline-flex items-center gap-1">
                        <SeverityGlyph severity={severity} />
                        <span className="text-text-dim">{e.hybrid?.band ?? (severity === "critical" ? "confirmed" : "anomaly")}</span>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------

function StatBlock({ label, value, valueClassName }: { label: string; value: string; valueClassName?: string }) {
  return (
    <div className="rounded-[var(--radius-dense)] border border-glass-border bg-glass-raised px-3 py-2">
      <p className="text-[10px] font-medium uppercase tracking-[0.06em] text-text-mute">{label}</p>
      <p className={`font-mono text-lg font-semibold tabular-nums ${valueClassName ?? "text-text"}`}>{value}</p>
    </div>
  );
}

function LoadingRow({ label }: { label: string }) {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 py-8 text-text-dim">
      <span className="h-5 w-5 animate-spin rounded-full border-2 border-glass-border-strong border-t-accent" aria-hidden="true" />
      <p className="text-sm">{label}</p>
    </div>
  );
}

function ErrorRow({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 py-8 text-center">
      <p className="text-sm font-medium text-sev-critical">Unable to load</p>
      <p className="font-mono text-xs text-text-mute">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-1 rounded-[var(--radius-dense)] border border-glass-border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim hover:border-accent hover:text-accent"
      >
        Retry
      </button>
    </div>
  );
}

function EmptyRow({ message }: { message: string }) {
  return (
    <div className="flex flex-1 items-center justify-center py-8 text-center">
      <p className="max-w-md text-sm text-text-mute">{message}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panel shell with tabs
// ---------------------------------------------------------------------------

type Tab = "prevention" | "detection";

export function DetectionPreventionPanel() {
  const [tab, setTab] = useState<Tab>("prevention");

  return (
    <Panel
      label="Detection & Prevention"
      className="min-h-[360px]"
      bodyClassName="flex min-h-0 flex-1 flex-col gap-3"
      action={
        <div className="flex gap-1 rounded-[var(--radius-dense)] border border-glass-border p-0.5">
          <TabButton active={tab === "prevention"} onClick={() => setTab("prevention")}>
            Prevention (IPS)
          </TabButton>
          <TabButton active={tab === "detection"} onClick={() => setTab("detection")}>
            Detection (IDS)
          </TabButton>
        </div>
      }
    >
      {tab === "prevention" ? <PreventionTab /> : <DetectionTab />}
    </Panel>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-[calc(var(--radius-dense)-2px)] px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.05em] transition-colors ${
        active ? "bg-accent text-white" : "text-text-dim hover:text-text"
      }`}
    >
      {children}
    </button>
  );
}
