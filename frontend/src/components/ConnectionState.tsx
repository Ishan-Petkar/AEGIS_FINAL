"use client";

import { useConnection, type ConnectionStatus } from "@/lib/connection-context";

const STATUS_META: Record<
  ConnectionStatus,
  { label: string; dotClass: string; textClass: string }
> = {
  connecting: {
    label: "Connecting",
    dotClass: "bg-text-mute",
    textClass: "text-text-mute",
  },
  connected: {
    label: "Connected",
    dotClass: "bg-sev-normal animate-aegis-pulse",
    textClass: "text-sev-normal",
  },
  degraded: {
    label: "Degraded",
    dotClass: "bg-sev-warning animate-aegis-pulse",
    textClass: "text-sev-warning",
  },
  unreachable: {
    label: "Unreachable",
    dotClass: "bg-sev-critical",
    textClass: "text-sev-critical",
  },
};

/**
 * ConnectionState — header indicator driven by `ConnectionProvider`
 * (`@/lib/connection-context`), which owns the GET /api/health poll
 * (Ticket #3 scope, not a WebSocket — that's Ticket #4/#12). This
 * component only renders; it does not poll on its own, so it can never
 * disagree with any other panel reading the same context (see MEDIUM-1
 * in the Ticket #3 fix round).
 */
export function ConnectionState() {
  const { status, health } = useConnection();

  const meta = STATUS_META[status];
  const title =
    status === "unreachable"
      ? `Cannot reach AEGIS backend`
      : health?.scorer_load_error
        ? `Scorer: ${health.scorer_load_error}`
        : undefined;

  return (
    <div
      className="flex items-center gap-2"
      title={title}
      aria-live="polite"
    >
      <span
        className={`inline-block h-2 w-2 shrink-0 rounded-full ${meta.dotClass}`}
        aria-hidden="true"
      />
      <span className={`text-[11px] font-semibold uppercase tracking-[0.08em] ${meta.textClass}`}>
        {meta.label}
      </span>
    </div>
  );
}
