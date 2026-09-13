"use client";

import { useState } from "react";
import type { DiscoverResponse, UADAResponse } from "@/lib/types";
import SqlView from "./SqlView";
import SchemaView from "./SchemaView";
import DataView from "./DataView";

type Tab = "sql" | "schema" | "data";

export default function RightPanel({
  sql,
  connectionId,
  discovery,
  lastResponse,
}: {
  sql: string | null;
  connectionId: string | null;
  discovery: DiscoverResponse | null;
  lastResponse: UADAResponse | null;
}) {
  const [tab, setTab] = useState<Tab>("sql");

  const tabs: { key: Tab; label: string }[] = [
    { key: "sql", label: "SQL" },
    { key: "schema", label: "Schema" },
    { key: "data", label: "Data" },
  ];

  return (
    <aside className="flex w-[340px] flex-shrink-0 flex-col border-l border-border bg-surface">
      <div className="flex border-b border-border">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex-1 border-b-2 py-3 text-center text-xs font-semibold transition-colors ${
              tab === t.key
                ? "border-accent text-accent"
                : "border-transparent text-muted hover:text-text"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto p-3.5">
        {tab === "sql" && <SqlView sql={sql} hasRun={lastResponse != null} />}
        {tab === "schema" && <SchemaView connectionId={connectionId} discovery={discovery} />}
        {tab === "data" && <DataView response={lastResponse} />}
      </div>
    </aside>
  );
}
