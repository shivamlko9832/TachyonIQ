import type { AnomalyResult } from "@/lib/types";

export default function AnomalyList({ an }: { an: AnomalyResult }) {
  if (an.skipped || !an.anomaly_count) return null;
  const top = an.anomaly_rows.filter((r) => r.is_anomaly).slice(0, 3);

  return (
    <div className="mt-2">
      <div className="inline-flex items-center gap-1.5 rounded-full border border-danger/30 bg-danger/[0.12] px-2.5 py-1 text-xs font-semibold text-danger">
        {"⚠"} {an.anomaly_count} anomal{an.anomaly_count === 1 ? "y" : "ies"} detected
      </div>
      {top.map((row) => {
        const devs = Object.entries(row.column_deviations)
          .slice(0, 4)
          .map(([k, v]) => `${k}: ${v >= 0 ? "+" : ""}${v.toFixed(2)}σ`)
          .join(" · ");
        return (
          <div
            key={row.row_index}
            className="mt-1 rounded border-l-2 border-danger bg-danger/5 px-2 py-1 text-xs text-muted"
          >
            row {row.row_index}: {devs}
          </div>
        );
      })}
    </div>
  );
}
