"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "./Panel";
import { SeverityGlyph, type Severity } from "./SeverityGlyph";
import { ApiError, ApiNetworkError, getIpsActions, getIpsPolicy, rollbackIpsAction } from "@/lib/api";
import { useConnection } from "@/lib/connection-context";
import { useStream } from "@/lib/stream-context";
import type { EventEnvelopeData, IpsActionEnvelopeData, IpsActionOut, IpsPolicyResponse } from "@/lib/types";

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

/** Merge live WS ipsActions into the REST list: newest lifecycle state
 * per id wins (a rollback envelope must beat the original decision entry). */
function mergeIpsActions(rest: IpsActionOut[], live: IpsActionEnvelopeData[]): IpsActionOut[] {
  const map = new Map<number, IpsActionOut>();
  // REST first (base)
  for (const a of rest) map.set(a.id, a);
  // Live overwrites any stale REST copy (same id, newer lifecycle state)
  for (const a of live) {
    const existing = map.get(a.id);
    map.set(a.id, {
      ...(existing ?? { replay_session_id: null }),
      ...a,
    });
  }
  return [...map.values()].sort((a, b) => b.id - a.id);
}

/** Remaining seconds until `expires_at`, or null. */
function useTtl(expiresAt: string | null): number | null {
  const [remaining, setRemaining] = useState<number | null>(() => {
    if (!expiresAt) return null;
    return Math.max(0, Math.round((new Date(expiresAt).getTime() - Date.now()) / 1000));
  });
  useEffect(() => {
    if (!expiresAt) return;
    const tick = () => {
      const r = Math.max(0, Math.round((new Date(expiresAt).getTime() - Date.now()) / 1000));
      setRemaining(r);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [expiresAt]);
  return remaining;
}

function TtlBadge({ expiresAt }: { expiresAt: string | null }) {
  const remaining = useTtl(expiresAt);
  if (remaining === null) return null;
  const tone = remaining <= 30 ? "text-sev-warning" : "text-text-mute";
  return (
    <span className={`font-mono text-[10px] tabular-nums ${tone}`} title={`Expires at ${expiresAt ?? ""}`}>
      TTL {remaining}s
    </span>
  );
}

interface RollbackState {
  pending: boolean;
  error: string | null;
}

function PreventionRow({
  a,
  onRollback,
}: {
  a: IpsActionOut;
  onRollback: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [rb, setRb] = useState<RollbackState>({ pending: false, error: null });

  async function handleRollback() {
    setRb({ pending: true, error: null });
    try {
      await rollbackIpsAction(a.id);
      onRollback(a.id);
      setRb({ pending: false, error: null });
    } catch (err) {
      const msg = friendlyError(err, "Rollback failed — unknown error.");
      setRb({ pending: false, error: msg });
      onRollback(a.id); // refetch anyway — a 409 means it's already inactive
    }
  }

  const active = isActivePrevention(a);
  const isTerminal = !!a.rolled_back_at;

  // Parse evidence for display
  const ev = a.evidence as Record<string, unknown> | null;

  return (
    <>
      <tr className="border-b border-glass-border last:border-0 hover:bg-glass-raised">
        <td className="whitespace-nowrap px-3 py-2 font-mono text-text-mute">{formatTime(a.ts)}</td>
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
        <td className="px-3 py-2 text-text-dim">
          <div className="flex flex-col gap-0.5">
            <span>{a.status}</span>
            {active && <TtlBadge expiresAt={a.expires_at} />}
            {isTerminal && a.rolled_back_at && (
              <span className="font-mono text-[10px] text-text-mute">
                rolled back {new Date(a.rolled_back_at).toLocaleTimeString()}
              </span>
            )}
          </div>
        </td>
        <td className="px-3 py-2 text-right">
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className="rounded-[var(--radius-dense)] border border-glass-border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-text-mute hover:border-accent hover:text-accent"
              aria-label={open ? "Collapse detail" : "Expand detail"}
            >
              {open ? "▲" : "▼"}
            </button>
            {active && (
              <button
                type="button"
                disabled={rb.pending}
                onClick={handleRollback}
                className="rounded-[var(--radius-dense)] border border-glass-border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-text-dim transition-colors hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
              >
                {rb.pending ? "Rolling back…" : "Roll back"}
              </button>
            )}
          </div>
        </td>
      </tr>
      {rb.error && (
        <tr className="border-b border-glass-border">
          <td colSpan={6} className="px-3 py-1 text-xs text-sev-critical" role="alert">
            {rb.error}
          </td>
        </tr>
      )}
      {open && (
        <tr className="border-b border-glass-border bg-glass-raised/50">
          <td colSpan={6} className="px-3 py-2 text-xs text-text-dim">
            <div className="flex flex-col gap-1 font-mono">
              <p>
                <span className="text-text-mute">reason:</span> {a.reason}
              </p>
              {a.triggering_event_id != null && (
                <p>
                  <span className="text-text-mute">trigger event:</span> #{a.triggering_event_id}
                </p>
              )}
              {isTerminal && a.rollback_reason && (
                <p>
                  <span className="text-text-mute">rollback reason:</span> {a.rollback_reason}
                </p>
              )}
              {ev && (
                <div className="mt-1">
                  <p className="text-text-mute">evidence:</p>
                  <ul className="pl-3">
                    {Object.entries(ev).map(([k, v]) => (
                      <li key={k}>
                        {k}: {String(v)}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function PreventionTab() {
  const [restActions, setRestActions] = useState<IpsActionOut[]>([]);
  const [policy, setPolicy] = useState<IpsPolicyResponse | null>(null);
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [retryToken, setRetryToken] = useState(0);
  const { reconnectEpoch } = useConnection();
  // Live IPS action stream — merged with REST by id; newest lifecycle wins
  const { ipsActions: liveIpsActions } = useStream();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setState({ kind: "loading" });
      try {
        const [actionsResp, policyResp] = await Promise.all([
          // Pass active=true so activeCount is accurate past the 100-row page
          getIpsActions({ limit: HISTORY_LIMIT, active: true }),
          getIpsPolicy(),
        ]);
        if (cancelled) return;
        setRestActions(actionsResp.actions);
        setPolicy(policyResp);
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

  // Merge REST + live; live ipsActions also includes the full REST history
  // load — newest state per id wins so a rollback envelope beats the original
  const actions = useMemo(() => mergeIpsActions(restActions, liveIpsActions), [restActions, liveIpsActions]);

  const activeCount = useMemo(() => actions.filter(isActivePrevention).length, [actions]);

  const protectionLevel = !policy ? "—" : !policy.enabled ? "Disabled" : policy.dry_run ? "Dry-run" : "Enforcing";
  const protectionTone = !policy || !policy.enabled ? "text-text-mute" : policy.dry_run ? "text-sev-warning" : "text-sev-critical";

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="grid grid-cols-3 gap-3">
        <StatBlock label="Active Mitigations" value={String(activeCount)} />
        <StatBlock label="Recent Decisions" value={String(actions.length)} />
        <StatBlock label="Protection Mode" value={protectionLevel} valueClassName={protectionTone} />
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
          <table className="w-full min-w-[720px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-glass-border bg-glass-raised text-[10px] uppercase tracking-[0.06em] text-text-mute">
                <th className="px-3 py-2 font-semibold">Time</th>
                <th className="px-3 py-2 font-semibold">Target Asset</th>
                <th className="px-3 py-2 font-semibold">Action</th>
                <th className="px-3 py-2 font-semibold">Confidence</th>
                <th className="px-3 py-2 font-semibold">Status / TTL</th>
                <th className="px-3 py-2 font-semibold" />
              </tr>
            </thead>
            <tbody>
              {actions.map((a) => (
                <PreventionRow
                  key={a.id}
                  a={a}
                  onRollback={() => setRetryToken((n) => n + 1)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
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
                  <tr key={e.id} className="border-b border-glass-border last:border-0 hover:bg-glass-raised">
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
