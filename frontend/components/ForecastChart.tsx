import type { ForecastResult } from "@/lib/types";
import ChartView from "./ChartView";

export default function ForecastChart({ fc }: { fc: ForecastResult }) {
  if (fc.skipped || !fc.points.length) return null;
  const points = fc.points.slice(0, 12);

  const spec = {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    width: "container",
    height: 100,
    background: "transparent",
    data: { values: points.map((p, i) => ({ i, period: p.period, forecast: p.forecast })) },
    layer: [
      {
        mark: { type: "area", color: "rgba(13,148,136,0.18)", line: { color: "#0d9488" } },
        encoding: {
          x: { field: "i", type: "quantitative", axis: null },
          y: { field: "forecast", type: "quantitative", axis: { tickCount: 3 } },
        },
      },
    ],
    config: { view: { stroke: null } },
  };

  return (
    <div>
      <div className="mb-1 text-xs text-muted">
        Forecast {fc.method ? `· ${fc.method}` : ""} {"·"} next {points.length} periods
      </div>
      <ChartView spec={spec} />
    </div>
  );
}
