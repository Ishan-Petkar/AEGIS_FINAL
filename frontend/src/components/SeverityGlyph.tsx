export type Severity = "critical" | "warning" | "normal" | "info";

const SEVERITY_COLOR: Record<Severity, string> = {
  critical: "var(--sev-critical)",
  warning: "var(--sev-warning)",
  normal: "var(--sev-normal)",
  info: "var(--sev-info)",
};

const SEVERITY_LABEL: Record<Severity, string> = {
  critical: "Critical",
  warning: "Warning",
  normal: "Normal",
  info: "Info",
};

interface SeverityGlyphProps {
  severity: Severity;
  className?: string;
}

/**
 * Geometric status glyph — DESIGN_CONSOLE.md §6: "● normal, ▲ warning,
 * ■ critical ... no emoji." Severity is never color-only (§7): the glyph
 * shape itself carries the meaning, and callers should still pair it with
 * a text label for screen readers / colorblind operators.
 */
export function SeverityGlyph({ severity, className = "" }: SeverityGlyphProps) {
  const color = SEVERITY_COLOR[severity];
  const label = SEVERITY_LABEL[severity];

  return (
    <svg
      viewBox="0 0 12 12"
      width="10"
      height="10"
      className={className}
      role="img"
      aria-label={label}
    >
      {severity === "critical" && (
        <rect x="1.5" y="1.5" width="9" height="9" fill={color} />
      )}
      {severity === "warning" && <polygon points="6,1.5 10.5,10 1.5,10" fill={color} />}
      {(severity === "normal" || severity === "info") && (
        <circle cx="6" cy="6" r="4.5" fill={color} />
      )}
    </svg>
  );
}
