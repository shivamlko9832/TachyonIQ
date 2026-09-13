import type { KpiSpec } from "@/lib/types";

export default function KpiCard({ viz }: { viz: KpiSpec }) {
  const direction = viz.trend_direction || "flat";
  const arrow = direction === "up" ? "↑" : direction === "down" ? "↓" : "→";
  const trendColor =
    direction === "up" ? "text-up" : direction === "down" ? "text-down" : "text-muted";

  return (
    <div className="mt-2 inline-flex min-w-[180px] flex-col gap-1 rounded-xl border border-border bg-surface-2 px-6 py-5">
      <div className="text-xs uppercase tracking-wide text-muted">{viz.label}</div>
      <div className="text-4xl font-bold leading-none tracking-tight text-text">
        {viz.formatted_value ?? viz.value}
      </div>
      {viz.trend_pct != null && (
        <div className={`mt-1 text-sm ${trendColor}`}>
          {arrow} {Math.abs(viz.trend_pct).toFixed(1)}% {viz.comparison_label || ""}
        </div>
      )}
    </div>
  );
}
