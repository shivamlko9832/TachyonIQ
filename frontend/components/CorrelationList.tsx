import type { CorrelationResult } from "@/lib/types";

export default function CorrelationList({ corr }: { corr: CorrelationResult }) {
  if (corr.skipped || !corr.top_pairs.length) return null;
  const pairs = corr.top_pairs.slice(0, 5);

  return (
    <div className="mt-2">
      <div className="mb-1 text-xs text-muted">Top Correlations</div>
      {pairs.map((p, i) => {
        const val = typeof p.correlation === "number" ? p.correlation : 0;
        const pct = Math.min(100, Math.abs(val * 100));
        const positive = val >= 0;
        return (
          <div key={i} className="flex items-center gap-2 py-0.5 text-xs">
            <div className="min-w-[130px] truncate text-text">
              {p.col_a} {"↔"} {p.col_b}
            </div>
            <div className="h-1 flex-1 overflow-hidden rounded bg-border">
              <div
                className={`h-full rounded ${positive ? "bg-up" : "bg-danger"}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="min-w-[34px] text-right text-muted">
              {positive ? "+" : ""}
              {val.toFixed(2)}
            </div>
          </div>
        );
      })}
    </div>
  );
}
