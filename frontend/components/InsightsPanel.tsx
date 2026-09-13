import type { GeneratedInsights } from "@/lib/types";

function Bullets({ label, items }: { label: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div className="mb-2 last:mb-0">
      <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
        {label}
      </div>
      {items.map((item, i) => (
        <div key={i} className="py-0.5 text-xs leading-relaxed text-text">
          <span className="text-accent2">{"• "}</span>
          {item}
        </div>
      ))}
    </div>
  );
}

export default function InsightsPanel({ ins }: { ins: GeneratedInsights }) {
  const hasSomething =
    ins.key_findings.length || ins.drivers.length || ins.recommendations.length;
  if (!hasSomething) return null;

  return (
    <div className="mt-2 rounded-xl border border-accent2/20 bg-accent2/[0.05] px-3.5 py-3">
      <div className="mb-2 text-[10px] font-bold uppercase tracking-wide text-accent2">
        {"✨"} AI Insights
      </div>
      <Bullets label="Key Findings" items={ins.key_findings} />
      <Bullets label="Drivers" items={ins.drivers} />
      <Bullets label="Recommendations" items={ins.recommendations} />
      {ins.confidence && (
        <div className="mt-1.5 text-[10px] italic text-muted">Confidence: {ins.confidence}</div>
      )}
    </div>
  );
}
