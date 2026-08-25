import { Panel } from "./Panel";
import { SeverityGlyph, type Severity } from "./SeverityGlyph";

interface PlaceholderAlert {
  severity: Severity;
  asset: string;
  why: string;
  blastRadius: string[];
  acknowledged: boolean;
}

// Static placeholder content only — Ticket #3 scope. Real /api/alerts
// wiring and ACK POST are Ticket #15.
const PLACEHOLDER_ALERTS: PlaceholderAlert[] = [
  {
    severity: "critical",
    asset: "Payment_Gateway",
    why: "Outbound transaction volume 47σ above 5-minute baseline.",
    blastRadius: ["Core_Banking_DB", "Interbank_Settlement", "Fraud_Analytics"],
    acknowledged: false,
  },
  {
    severity: "warning",
    asset: "SCADA_HMI_02",
    why: "Unusual write-command cadence to PLC_Water_01 (3x normal rate).",
    blastRadius: ["PLC_Water_01", "Water_Treatment_Plant"],
    acknowledged: false,
  },
  {
    severity: "normal",
    asset: "Camera_North_04",
    why: "Repeated auth failures from an unrecognized source IP, then resolved.",
    blastRadius: ["Camera_North_04"],
    acknowledged: true,
  },
];

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

/**
 * AlertsRail (DESIGN_CONSOLE.md §5, §6) — stacked alert cards, one
 * critical / one warning / one acknowledged, each with a realistic "why"
 * line and a collapsible blast-radius list. The ACK button is present,
 * styled, and focusable but intentionally non-functional here — wiring
 * it to POST /api/alerts/{id}/ack is Ticket #15.
 */
export function AlertsRail() {
  return (
    <Panel
      label="Active Alerts"
      className="w-full lg:w-[380px] lg:shrink-0"
      bodyClassName="flex flex-col gap-3 overflow-y-auto"
    >
      {PLACEHOLDER_ALERTS.map((alert, i) => (
        <article
          key={`${alert.asset}-${i}`}
          className={`glass-panel border-l-2 p-3 ${SEVERITY_BORDER[alert.severity]} ${
            alert.acknowledged ? "opacity-55" : ""
          }`}
        >
          <div className="flex items-center gap-2">
            <SeverityGlyph severity={alert.severity} />
            <span
              className={`text-[10px] font-semibold uppercase tracking-[0.08em] ${SEVERITY_TEXT[alert.severity]}`}
            >
              {alert.severity}
            </span>
            <span className="ml-auto font-mono text-xs text-text-mute">
              {alert.asset}
            </span>
          </div>

          <p className="mt-2 text-sm text-text">{alert.why}</p>

          <details className="mt-2 group">
            <summary className="cursor-pointer text-[11px] uppercase tracking-[0.06em] text-text-dim transition-colors duration-150 ease-out hover:text-text [&::-webkit-details-marker]:hidden">
              Blast radius ({alert.blastRadius.length})
            </summary>
            <ul className="mt-1 flex flex-col gap-0.5 pl-3 font-mono text-xs text-text-dim">
              {alert.blastRadius.map((asset) => (
                <li key={asset}>{asset}</li>
              ))}
            </ul>
          </details>

          <div className="mt-3 flex justify-end">
            <button
              type="button"
              disabled={alert.acknowledged}
              aria-label={
                alert.acknowledged
                  ? `${alert.asset} alert already acknowledged`
                  : `Acknowledge ${alert.asset} alert (not yet wired — Ticket #15)`
              }
              title="Ack wiring lands in Ticket #15"
              className="rounded-[var(--radius-dense)] border border-glass-border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim transition-colors duration-150 ease-out hover:bg-glass-raised hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
            >
              {alert.acknowledged ? "Acked" : "Ack"}
            </button>
          </div>
        </article>
      ))}
    </Panel>
  );
}
