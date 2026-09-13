"use client";

import { useEffect, useRef } from "react";

export default function ChartView({ spec }: { spec: Record<string, unknown> }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    let result: { finalize?: () => void } | undefined;
    let cancelled = false;

    (async () => {
      const vegaEmbed = (await import("vega-embed")).default;
      if (cancelled || !ref.current) return;
      try {
        result = await vegaEmbed(ref.current, spec, {
          actions: false,
          renderer: "svg",
          config: {
            background: "transparent",
            view: { stroke: "transparent" },
            axis: {
              domainColor: "#e5e7eb",
              gridColor: "#f1f2f5",
              tickColor: "#e5e7eb",
              labelColor: "#6b7280",
              titleColor: "#374151",
              labelFont: "inherit",
              titleFont: "inherit",
            },
            legend: { labelColor: "#6b7280", titleColor: "#374151", labelFont: "inherit" },
            range: {
              category: [
                "#111827",
                "#0d9488",
                "#6366f1",
                "#d97706",
                "#16a34a",
                "#dc2626",
                "#0ea5e9",
                "#9333ea",
              ],
            },
            mark: { color: "#111827" },
            bar: { color: "#111827" },
            line: { color: "#111827", strokeWidth: 2 },
            point: { color: "#111827", filled: true },
            area: { color: "#111827", opacity: 0.15 },
          },
        });
      } catch {
        /* leave the container empty on a malformed spec */
      }
    })();

    return () => {
      cancelled = true;
      result?.finalize?.();
    };
  }, [spec]);

  return (
    <div className="mt-2 rounded-lg border border-border bg-surface p-3">
      <div ref={ref} />
    </div>
  );
}
