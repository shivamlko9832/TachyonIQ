import type { ConnectionSummary } from "@/lib/types";

export default function Sidebar({
  connections,
  activeConnectionId,
  onSelect,
  onAddClick,
  history,
}: {
  connections: ConnectionSummary[];
  activeConnectionId: string | null;
  onSelect: (c: ConnectionSummary) => void;
  onAddClick: () => void;
  history: string[];
}) {
  return (
    <nav className="flex w-[260px] flex-shrink-0 flex-col border-r border-border bg-surface">
      <div className="flex items-center gap-1.5 border-b border-border px-5 py-4">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/logo.webp" alt="Tachyon" className="h-6 w-auto" />
        <span className="text-[15px] font-extrabold tracking-tight text-text">IQ</span>
      </div>

      <div className="px-4 pb-1.5 pt-4 text-[11px] font-semibold uppercase tracking-wider text-muted">
        Connections
      </div>
      <div className="px-2">
        {connections.length === 0 && (
          <div className="px-2.5 py-2 text-xs text-muted">No connections yet</div>
        )}
        {connections.map((c) => {
          const active = c.connection_id === activeConnectionId;
          return (
            <div
              key={c.connection_id}
              onClick={() => onSelect(c)}
              className={`mb-0.5 flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors ${
                active ? "bg-accent text-accent-fg" : "text-text hover:bg-surface-2"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${
                  c.last_test_ok === true
                    ? "bg-up"
                    : c.last_test_ok === false
                      ? "bg-danger"
                      : active
                        ? "bg-white/60"
                        : "bg-muted"
                }`}
              />
              <span className="flex-1 truncate" title={c.name}>
                {c.name}
              </span>
              <span
                className={`rounded-full px-1.5 py-0.5 text-[10px] ${
                  active ? "bg-white/15 text-white" : "bg-surface-2 text-muted"
                }`}
              >
                {c.dialect}
              </span>
            </div>
          );
        })}
      </div>

      <div className="px-2 pt-1">
        <button
          onClick={onAddClick}
          className="flex w-full items-center gap-2.5 rounded-lg border border-dashed border-border px-2.5 py-2 text-sm text-muted transition-colors hover:border-accent hover:text-accent"
        >
          <span className="text-base leading-none">+</span>
          <span>Add Connection</span>
        </button>
      </div>

      <div className="mt-3 px-4 pb-1.5 pt-3 text-[11px] font-semibold uppercase tracking-wider text-muted">
        History
      </div>
      <div className="flex-1 overflow-y-auto px-2">
        {history.map((q, i) => (
          <div
            key={i}
            title={q}
            className={`mb-0.5 truncate rounded-lg px-2.5 py-1.5 text-xs transition-colors hover:bg-surface-2 ${
              i === 0 ? "bg-surface-2 text-text" : "text-muted"
            }`}
          >
            {q}
          </div>
        ))}
      </div>

      <div className="mt-auto border-t border-border px-5 py-3 text-[11px] text-muted">
        v0.1 · TachyonIQ
      </div>
    </nav>
  );
}
