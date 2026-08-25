import { Panel } from "./Panel";
import { SeverityGlyph, type Severity } from "./SeverityGlyph";

interface PlaceholderRow {
  time: string;
  src: string;
  dst: string;
  severity: Severity;
}

// Static placeholder content only — Ticket #3 scope. Live feed behaviour
// (autoscroll, 200-row cap, real /api/events wiring) is Ticket #10.
const PLACEHOLDER_ROWS: PlaceholderRow[] = [
  { time: "09:34:02.140", src: "10.0.1.14", dst: "10.0.1.1", severity: "normal" },
  { time: "09:34:02.512", src: "10.0.1.22", dst: "10.0.2.5", severity: "normal" },
  { time: "09:34:03.001", src: "10.0.1.14", dst: "PLC_Water_01", severity: "warning" },
  { time: "09:34:03.340", src: "10.0.2.5", dst: "10.0.1.1", severity: "normal" },
  { time: "09:34:03.882", src: "Payment_Gateway", dst: "Core_Banking_DB", severity: "normal" },
  { time: "09:34:04.117", src: "10.0.1.31", dst: "Camera_North_04", severity: "normal" },
  { time: "09:34:04.605", src: "External_IP_203.0.113.9", dst: "10.0.1.1", severity: "critical" },
  { time: "09:34:04.981", src: "10.0.2.5", dst: "10.0.2.9", severity: "normal" },
  { time: "09:34:05.226", src: "10.0.1.22", dst: "SCADA_HMI_02", severity: "warning" },
  { time: "09:34:05.703", src: "10.0.1.14", dst: "10.0.1.1", severity: "normal" },
  { time: "09:34:06.055", src: "10.0.2.9", dst: "Core_Banking_DB", severity: "normal" },
  { time: "09:34:06.412", src: "10.0.1.31", dst: "10.0.1.1", severity: "normal" },
];

/**
 * TelemetryRail (DESIGN_CONSOLE.md §5, §6) — feed rows, 28px tall, mono,
 * `time · src → dst · glyph`. Fresh rows at full opacity fading to
 * `--text-mute` with age; anomalous rows get a 2px left border in their
 * severity color. Static placeholder data only for this ticket.
 */
export function TelemetryRail() {
  return (
    <Panel label="Telemetry" className="w-full lg:w-[340px] lg:shrink-0" bodyClassName="overflow-y-auto">
      <ul className="flex flex-col">
        {PLACEHOLDER_ROWS.map((row, i) => {
          const isAnomalous = row.severity !== "normal";
          const opacity = Math.max(0.4, 1 - i * 0.05);
          return (
            <li
              key={`${row.time}-${i}`}
              className={`flex h-7 items-center gap-2 border-b border-glass-border/50 pl-2 font-mono text-xs tabular-nums ${
                isAnomalous ? "border-l-2" : "border-l-2 border-l-transparent"
              }`}
              style={{
                opacity,
                borderLeftColor: isAnomalous
                  ? row.severity === "critical"
                    ? "var(--sev-critical)"
                    : "var(--sev-warning)"
                  : undefined,
              }}
            >
              <span className="text-text-mute">{row.time}</span>
              <span className="truncate text-text-dim">
                {row.src} <span aria-hidden="true">&rarr;</span> {row.dst}
              </span>
              <SeverityGlyph severity={row.severity} className="ml-auto shrink-0" />
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
