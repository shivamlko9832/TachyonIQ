"use client";

import { useState } from "react";
import { highlightSql } from "@/lib/format";

export default function SqlView({ sql, hasRun }: { sql: string | null; hasRun: boolean }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    if (!sql) return;
    await navigator.clipboard.writeText(sql);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div>
      <div className="mb-2 flex justify-end">
        <button
          onClick={copy}
          disabled={!sql}
          className="rounded border border-border px-2.5 py-1 text-[11px] text-muted transition-colors hover:border-accent hover:text-accent disabled:opacity-40"
        >
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
      {sql ? (
        <pre
          className="whitespace-pre-wrap break-all rounded-lg border border-border bg-surface-2 p-3 font-mono text-xs leading-relaxed text-text"
          dangerouslySetInnerHTML={{ __html: highlightSql(sql) }}
        />
      ) : (
        <div className="py-8 text-center text-sm text-muted">
          {hasRun ? "SQL is hidden for your role." : "Run a query to see SQL"}
        </div>
      )}
    </div>
  );
}
