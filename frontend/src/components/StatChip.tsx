export type SemanticTone =
  | "accent"
  | "critical"
  | "warning"
  | "normal"
  | "info"
  | "text";

const TONE_CLASS: Record<SemanticTone, string> = {
  accent: "text-accent",
  critical: "text-sev-critical",
  warning: "text-sev-warning",
  normal: "text-sev-normal",
  info: "text-sev-info",
  text: "text-text",
};

interface StatChipProps {
  label: string;
  value: string;
  tone?: SemanticTone;
}

/**
 * StatChip (DESIGN_CONSOLE.md §6) — mono value + uppercase micro-label.
 * The value is colored by its own semantic scale, never by `--accent`
 * (brand color is reserved for interactive elements per §2).
 */
export function StatChip({ label, value, tone = "text" }: StatChipProps) {
  return (
    <div className="flex flex-col items-start gap-0.5">
      <span
        className={`font-mono text-base font-semibold tabular-nums leading-none ${TONE_CLASS[tone]}`}
      >
        {value}
      </span>
      <span className="text-[10px] font-medium uppercase leading-none tracking-[0.08em] text-text-dim">
        {label}
      </span>
    </div>
  );
}
