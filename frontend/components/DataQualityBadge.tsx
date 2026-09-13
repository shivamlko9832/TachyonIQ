import type { DataQualityReport } from "@/lib/types";

const dotColor: Record<string, string> = {
  high: "bg-danger",
  medium: "bg-warn",
  low: "bg-muted",
};

export default function DataQualityBadge({ dq }: { dq: DataQualityReport }) {
  if (dq.skipped) return null;
  const score = dq.overall_quality_score ?? 100;
  const tone =
    score >= 80
      ? { badge: "bg-up/10 text-up border-up/25", icon: "✓" }
      : score >= 50
        ? { badge: "bg-warn/10 text-warn border-warn/25", icon: "⚠" }
        : { badge: "bg-danger/10 text-danger border-danger/25", icon: "✗" };

  return (
    <div className="mt-2">
      <div
        className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-semibold ${tone.badge}`}
      >
        {tone.icon} Data Quality: {score.toFixed(0)}%
      </div>
      {dq.summary && <div className="mt-1 text-xs text-muted">{dq.summary}</div>}
      {dq.issues.length > 0 && (
        <div className="mt-1 space-y-1">
          {dq.issues.slice(0, 4).map((issue, i) => (
            <div key={i} className="flex items-center gap-1.5 text-xs text-muted">
              <span className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${dotColor[issue.severity]}`} />
              <span>
                <b className="text-text">{issue.column}</b>: {issue.detail || issue.issue_type}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
