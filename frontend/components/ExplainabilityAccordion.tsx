"use client";

import { useState } from "react";
import type { ExplainabilityContext } from "@/lib/types";

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-2 last:mb-0">
      <div className="mb-0.5 text-[10px] font-bold uppercase tracking-wide text-text">
        {label}
      </div>
      <div>{children}</div>
    </div>
  );
}

export default function ExplainabilityAccordion({ ex }: { ex: ExplainabilityContext }) {
  const [open, setOpen] = useState(false);
  const hasBody =
    ex.calculation_steps.length ||
    ex.filters_applied.length ||
    ex.aggregations.length ||
    ex.tables_referenced.length ||
    ex.assumptions.length ||
    ex.chart_rationale;
  if (!hasBody) return null;

  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-border">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full select-none items-center justify-between bg-surface-2 px-3 py-2 text-xs font-semibold text-muted transition-colors hover:text-text"
      >
        <span>{"🔍"} How this was calculated</span>
        <span>{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-border px-3 py-2.5 text-xs leading-relaxed text-muted">
          {ex.calculation_steps.length > 0 && (
            <Section label="Calculation Steps">
              {ex.calculation_steps.map((s, i) => (
                <div key={i}>
                  <span className="text-accent">{"→"} </span>
                  {s}
                </div>
              ))}
            </Section>
          )}
          {ex.filters_applied.length > 0 && (
            <Section label="Filters">{ex.filters_applied.join(", ")}</Section>
          )}
          {ex.aggregations.length > 0 && (
            <Section label="Aggregations">{ex.aggregations.join(", ")}</Section>
          )}
          {ex.tables_referenced.length > 0 && (
            <Section label="Tables">{ex.tables_referenced.join(", ")}</Section>
          )}
          {ex.assumptions.length > 0 && (
            <Section label="Assumptions">{ex.assumptions.join("; ")}</Section>
          )}
          {ex.chart_rationale && <Section label="Chart Choice">{ex.chart_rationale}</Section>}
        </div>
      )}
    </div>
  );
}
