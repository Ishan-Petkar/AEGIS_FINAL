import type { ReactNode } from "react";

interface PanelProps {
  /** Uppercase 11px header label per DESIGN_CONSOLE.md §6. */
  label: string;
  /** Optional control(s) rendered at the right edge of the header row. */
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  /** Extra classes for the scrollable body wrapper. */
  bodyClassName?: string;
}

/**
 * Panel — the shared glass primitive (DESIGN_CONSOLE.md §6): glass fill,
 * 1px hairline border, 4px radius, 16px padding, uppercase label with a
 * hairline divider beneath.
 *
 * The header row (and its `border-b` divider) only renders when there is
 * a `label` or an `action` to put in it. Several NTV panels pass
 * `label=""` to use Panel purely as a bordered/padded container with no
 * heading — rendering an empty header for those still drew its bottom
 * divider, a stray horizontal line with nothing on either side of it
 * (visible cutting through NTVStatusBanner's content, which additionally
 * pulled itself upward with a negative margin straight across that
 * line). Skipping the header entirely for the label-less case removes
 * the divider at its source instead of hiding it per call site.
 */
export function Panel({
  label,
  action,
  children,
  className = "",
  bodyClassName = "",
}: PanelProps) {
  return (
    <section
      className={`glass-panel flex min-h-0 flex-col p-4 ${className}`}
      aria-label={label}
    >
      {(label || action) && (
        <header className="mb-3 flex shrink-0 items-center justify-between gap-3 border-b border-glass-border pb-2">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-dim">
            {label}
          </h2>
          {action}
        </header>
      )}
      <div className={`min-h-0 flex-1 ${bodyClassName}`}>{children}</div>
    </section>
  );
}
