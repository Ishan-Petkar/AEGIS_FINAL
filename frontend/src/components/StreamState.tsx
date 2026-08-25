import type { StreamStatus } from "@/lib/useEventStream";

const STATUS_META: Record<
  StreamStatus,
  { label: string; dotClass: string; textClass: string }
> = {
  connecting: {
    label: "Stream: Connecting",
    dotClass: "bg-text-mute",
    textClass: "text-text-mute",
  },
  connected: {
    label: "Stream: Live",
    dotClass: "bg-accent animate-aegis-pulse",
    textClass: "text-accent",
  },
  reconnecting: {
    label: "Stream: Reconnecting",
    dotClass: "bg-sev-warning animate-aegis-pulse",
    textClass: "text-sev-warning",
  },
  disconnected: {
    label: "Stream: Disconnected",
    dotClass: "bg-sev-critical",
    textClass: "text-sev-critical",
  },
};

interface StreamStateProps {
  status: StreamStatus;
}

/**
 * StreamState — header indicator for WebSocket stream connectivity
 * (Ticket #4), deliberately SEPARATE from `ConnectionState` (REST API
 * reachability via `ConnectionProvider`). D4-3: the two transports are
 * independent and must never be collapsed into one boolean — see
 * `useEventStream`'s module docstring. "Reconnecting" is only ever shown
 * while the hook actually has a reconnect timer scheduled.
 */
export function StreamState({ status }: StreamStateProps) {
  const meta = STATUS_META[status];

  return (
    <div className="flex items-center gap-2" aria-live="polite">
      <span
        className={`inline-block h-2 w-2 shrink-0 rounded-full ${meta.dotClass}`}
        aria-hidden="true"
      />
      <span
        className={`text-[11px] font-semibold uppercase tracking-[0.08em] ${meta.textClass}`}
      >
        {meta.label}
      </span>
    </div>
  );
}
