"use client";

import { useState } from "react";
import { createConnection, deleteConnection, ApiError } from "@/lib/api";
import type { ConnectionCreateRequest } from "@/lib/types";

const DIALECTS: { value: string; label: string; defaultPort: number }[] = [
  { value: "postgresql", label: "PostgreSQL", defaultPort: 5432 },
  { value: "mysql", label: "MySQL", defaultPort: 3306 },
  { value: "sqlite", label: "SQLite", defaultPort: 0 },
  { value: "mssql", label: "SQL Server", defaultPort: 1433 },
  { value: "duckdb", label: "DuckDB", defaultPort: 0 },
  { value: "snowflake", label: "Snowflake", defaultPort: 443 },
  { value: "bigquery", label: "BigQuery", defaultPort: 0 },
  { value: "redshift", label: "Redshift", defaultPort: 5439 },
];

const emptyForm = {
  name: "",
  dialect: "postgresql",
  host: "",
  port: 5432,
  database: "",
  username: "",
  password: "",
};

export default function ConnectionModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (connectionId: string) => void;
}) {
  const [form, setForm] = useState(emptyForm);
  const [status, setStatus] = useState<{ tone: "ok" | "err" | "info"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  function update<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function toBody(testOnCreate: boolean): ConnectionCreateRequest {
    return { ...form, test_on_create: testOnCreate };
  }

  async function handleTest() {
    setBusy(true);
    setStatus({ tone: "info", text: "Testing…" });
    try {
      const res = await createConnection(toBody(true));
      if (res.test_result?.ok) {
        setStatus({
          tone: "ok",
          text: `✓ Connected (${res.test_result.latency_ms?.toFixed(0)}ms) — ${
            res.test_result.server_version || ""
          }`,
        });
        await deleteConnection(res.connection_id);
      } else {
        setStatus({ tone: "err", text: `✗ ${res.test_result?.error || "Connection failed"}` });
      }
    } catch (e) {
      setStatus({ tone: "err", text: `✗ ${e instanceof ApiError ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  }

  async function handleSave() {
    setBusy(true);
    setStatus({ tone: "info", text: "Connecting…" });
    try {
      const res = await createConnection(toBody(false));
      setStatus({ tone: "ok", text: "✓ Connected!" });
      setTimeout(() => onCreated(res.connection_id), 500);
    } catch (e) {
      setStatus({ tone: "err", text: `✗ ${e instanceof ApiError ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/65"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-[440px] rounded-2xl border border-border bg-surface p-6 shadow-lg">
        <h3 className="mb-4 text-base font-bold">Add Database Connection</h3>

        <Field label="Connection Name">
          <input
            className="input"
            value={form.name}
            placeholder="My Analytics DB"
            onChange={(e) => update("name", e.target.value)}
          />
        </Field>

        <Field label="Dialect">
          <select
            className="input"
            value={form.dialect}
            onChange={(e) => {
              const d = DIALECTS.find((x) => x.value === e.target.value);
              update("dialect", e.target.value);
              if (d) update("port", d.defaultPort);
            }}
          >
            {DIALECTS.map((d) => (
              <option key={d.value} value={d.value}>
                {d.label}
              </option>
            ))}
          </select>
        </Field>

        <div className="mb-3 grid grid-cols-2 gap-2.5">
          <Field label="Host">
            <input
              className="input"
              value={form.host}
              placeholder="localhost"
              onChange={(e) => update("host", e.target.value)}
            />
          </Field>
          <Field label="Port">
            <input
              className="input"
              type="number"
              value={form.port}
              onChange={(e) => update("port", parseInt(e.target.value, 10) || 0)}
            />
          </Field>
        </div>

        <Field label="Database / File Path">
          <input
            className="input"
            value={form.database}
            placeholder="mydb"
            onChange={(e) => update("database", e.target.value)}
          />
        </Field>

        <div className="mb-3 grid grid-cols-2 gap-2.5">
          <Field label="Username">
            <input
              className="input"
              value={form.username}
              placeholder="user"
              onChange={(e) => update("username", e.target.value)}
            />
          </Field>
          <Field label="Password">
            <input
              className="input"
              type="password"
              value={form.password}
              placeholder="••••••••"
              onChange={(e) => update("password", e.target.value)}
            />
          </Field>
        </div>

        {status && (
          <div
            className={`min-h-[18px] text-xs ${
              status.tone === "ok" ? "text-up" : status.tone === "err" ? "text-danger" : "text-muted"
            }`}
          >
            {status.text}
          </div>
        )}

        <div className="mt-4 flex justify-end gap-2.5">
          <button
            onClick={onClose}
            className="rounded-md border border-border px-4 py-2 text-xs text-muted transition-colors hover:border-text hover:text-text"
          >
            Cancel
          </button>
          <button
            onClick={handleTest}
            disabled={busy || !form.name || !form.database}
            className="rounded-md bg-accent px-4 py-2 text-xs font-semibold text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            Test Connection
          </button>
          <button
            onClick={handleSave}
            disabled={busy || !form.name || !form.database}
            className="rounded-md bg-accent px-4 py-2 text-xs font-semibold text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            Connect
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <label className="mb-1 block text-xs text-muted">{label}</label>
      {children}
    </div>
  );
}
