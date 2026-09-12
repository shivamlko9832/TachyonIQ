import type { UADAResponse } from "@/lib/types";

const MAX_ROWS = 50;

export default function DataView({ response }: { response: UADAResponse | null }) {
  if (!response || response.row_count == null) {
    return <div className="py-8 text-center text-sm text-muted">Run a query to see result data</div>;
  }

  const qr = response.query_result;

  return (
    <div className="space-y-3 text-xs">
      <div className="space-y-2">
        <Row label="Row count" value={response.row_count} />
        <Row label="Truncated" value={response.is_truncated ? "Yes" : "No"} />
        {response.execution_time_ms != null && (
          <Row label="Execution time" value={`${response.execution_time_ms.toFixed(0)} ms`} />
        )}
        {response.complexity_tier && <Row label="Complexity tier" value={response.complexity_tier} />}
        {response.tables_used.length > 0 && (
          <Row label="Tables used" value={response.tables_used.join(", ")} />
        )}
      </div>

      {qr && qr.rows.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full border-collapse text-[11px]">
            <thead>
              <tr>
                {qr.columns.map((c) => (
                  <th
                    key={c.name}
                    className="whitespace-nowrap border-b border-border bg-surface-2 px-2 py-1.5 text-left font-semibold text-muted"
                  >
                    {c.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {qr.rows.slice(0, MAX_ROWS).map((row, i) => (
                <tr key={i} className="hover:bg-surface-2/60">
                  {row.map((value, j) => (
                    <td
                      key={j}
                      className="max-w-[140px] truncate whitespace-nowrap border-b border-border/60 px-2 py-1 text-text"
                      title={value == null ? "" : String(value)}
                    >
                      {value == null ? <span className="text-muted">—</span> : String(value)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {qr.rows.length > MAX_ROWS && (
            <div className="border-t border-border px-2 py-1.5 text-muted">
              Showing {MAX_ROWS} of {qr.rows.length} rows
            </div>
          )}
        </div>
      ) : (
        <div className="border-t border-border pt-3 text-muted">
          No result rows were returned for this response.
        </div>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex justify-between border-b border-border/60 py-1.5">
      <span className="text-muted">{label}</span>
      <span className="text-text">{value}</span>
    </div>
  );
}
