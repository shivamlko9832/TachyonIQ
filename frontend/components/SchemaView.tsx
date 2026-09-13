"use client";

import { useState } from "react";
import type { DiscoverResponse, SchemaRefreshResponse } from "@/lib/types";
import { browseSchema, ApiError } from "@/lib/api";

export default function SchemaView({
  connectionId,
  discovery,
}: {
  connectionId: string | null;
  discovery: DiscoverResponse | null;
}) {
  const [detail, setDetail] = useState<SchemaRefreshResponse | null>(null);
  const [openTable, setOpenTable] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!connectionId) {
    return <div className="py-8 text-center text-sm text-muted">Select a connection to browse schema</div>;
  }

  async function loadDetail() {
    if (!connectionId) return;
    setLoading(true);
    setError(null);
    try {
      const r = await browseSchema(connectionId);
      setDetail(r);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load schema details.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      {discovery && (
        <div className="mb-2.5 text-xs text-muted">
          {discovery.table_count} tables {"·"} {discovery.column_count} columns {"·"}{" "}
          {discovery.relationship_count} relationships
        </div>
      )}

      {!detail && (
        <button
          onClick={loadDetail}
          disabled={loading}
          className="w-full rounded-md border border-accent/30 bg-accent/[0.15] px-2 py-2 text-xs text-accent transition-colors hover:bg-accent/[0.28] disabled:opacity-50"
        >
          {loading ? "Loading columns…" : "Load Column Details"}
        </button>
      )}
      {error && <div className="mt-2 text-xs text-danger">{error}</div>}

      {detail && (
        <div className="mt-2 space-y-2">
          {detail.tables.map((t) => {
            const open = openTable === t.table_name;
            return (
              <div key={t.table_name}>
                <div
                  onClick={() => setOpenTable(open ? null : t.table_name)}
                  className="flex cursor-pointer items-center gap-1.5 text-xs font-semibold text-accent"
                >
                  <span>{open ? "▾" : "▸"}</span>
                  <span>{t.table_name}</span>
                  <span className="font-normal text-muted">
                    ({t.row_count != null ? `${t.row_count} rows, ` : ""}
                    {t.column_count} cols)
                  </span>
                </div>
                {open && (
                  <div className="mt-1 space-y-0.5 pl-4">
                    {t.columns.map((c) => (
                      <div
                        key={c.column_name}
                        className="flex justify-between text-[11px] text-muted"
                      >
                        <span>{c.column_name}</span>
                        <span className="text-border">{c.data_type}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
