import type { ChatMessage } from "@/lib/types";
import { formatTime } from "@/lib/format";
import { isKpiSpec, isVegaLiteSpec, isVisualisationFallback } from "@/lib/types";
import KpiCard from "./KpiCard";
import ChartView from "./ChartView";
import DataQualityBadge from "./DataQualityBadge";
import ExplainabilityAccordion from "./ExplainabilityAccordion";
import InsightsPanel from "./InsightsPanel";
import ForecastChart from "./ForecastChart";
import AnomalyList from "./AnomalyList";
import CorrelationList from "./CorrelationList";
import SuggestedQuestions from "./SuggestedQuestions";

export default function MessageBubble({
  msg,
  onPickQuestion,
}: {
  msg: ChatMessage;
  onPickQuestion: (q: string) => void;
}) {
  if (msg.role === "user") {
    return (
      <div className="flex max-w-[760px] flex-col items-end gap-1.5 self-end">
        <div className="rounded-2xl bg-accent px-4 py-2.5 text-sm leading-relaxed text-accent-fg">
          {msg.text}
        </div>
        <div className="text-[11px] text-muted">{formatTime(msg.createdAt)}</div>
      </div>
    );
  }

  if (msg.errorText) {
    return (
      <div className="flex max-w-[760px] flex-col gap-1.5">
        <div className="rounded-2xl border border-danger/30 bg-danger/5 px-4 py-2.5 text-xs text-danger shadow-card">
          {"⚠ "}
          {msg.errorText}
        </div>
        <div className="text-[11px] text-muted">{formatTime(msg.createdAt)}</div>
      </div>
    );
  }

  const r = msg.response;
  if (!r) return null;

  if (r.error) {
    return (
      <div className="flex max-w-[760px] flex-col gap-1.5">
        <div className="rounded-2xl border border-danger/30 bg-danger/5 px-4 py-2.5 text-xs text-danger shadow-card">
          {"⚠ "}
          {r.error.user_message || r.error.message || "Something went wrong."}
        </div>
        <div className="text-[11px] text-muted">{formatTime(msg.createdAt)}</div>
      </div>
    );
  }

  const viz = r.visualisation;

  return (
    <div className="flex max-w-[760px] flex-col gap-1.5">
      <div className="rounded-2xl border border-border bg-surface px-4 py-3.5 shadow-card">
        {r.answer && <div className="text-[15px] leading-relaxed text-text">{r.answer}</div>}
        {r.key_finding && (
          <div className="mt-1 text-xs text-muted">{"💡 "}{r.key_finding}</div>
        )}

        {isKpiSpec(viz) && <KpiCard viz={viz} />}
        {isVegaLiteSpec(viz) && <ChartView spec={viz.spec} />}
        {isVisualisationFallback(viz) && (
          <div className="mt-2 text-xs text-muted">No chart: {viz.reason}</div>
        )}

        {r.data_quality && <DataQualityBadge dq={r.data_quality} />}
        {r.explainability && <ExplainabilityAccordion ex={r.explainability} />}
        {r.generated_insights && <InsightsPanel ins={r.generated_insights} />}
        {r.forecast_result && <ForecastChart fc={r.forecast_result} />}
        {r.anomaly_result && <AnomalyList an={r.anomaly_result} />}
        {r.correlation_result && <CorrelationList corr={r.correlation_result} />}
      </div>

      {r.suggested_questions.length > 0 && (
        <SuggestedQuestions questions={r.suggested_questions} onPick={onPickQuestion} />
      )}

      <div className="text-[11px] text-muted">{formatTime(msg.createdAt)}</div>
    </div>
  );
}
