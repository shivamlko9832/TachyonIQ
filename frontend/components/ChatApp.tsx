"use client";

import { useEffect, useRef, useState } from "react";
import type { ChatMessage, ConnectionSummary, DiscoverResponse, UADAResponse } from "@/lib/types";
import { listConnections, discoverSchema, postQuery, ApiError } from "@/lib/api";
import { uid } from "@/lib/format";
import Sidebar from "./Sidebar";
import RightPanel from "./RightPanel";
import ConnectionModal from "./ConnectionModal";
import MessageBubble from "./MessageBubble";

export default function ChatApp() {
  const [connections, setConnections] = useState<ConnectionSummary[]>([]);
  const [activeConnectionId, setActiveConnectionId] = useState<string | null>(null);
  const [discovery, setDiscovery] = useState<DiscoverResponse | null>(null);

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [history, setHistory] = useState<string[]>([]);

  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);

  const chatRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    refreshConnections();
  }, []);

  useEffect(() => {
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight });
  }, [messages]);

  async function refreshConnections() {
    try {
      const { connections } = await listConnections();
      setConnections(connections);
    } catch {
      /* backend may not be reachable yet */
    }
  }

  async function selectConnection(c: ConnectionSummary) {
    setActiveConnectionId(c.connection_id);
    setDiscovery(null);
    setSessionId(null);
    setMessages([]);
    try {
      const d = await discoverSchema(c.connection_id);
      setDiscovery(d);
    } catch {
      /* schema discovery is best-effort */
    }
  }

  async function handleCreated(connectionId: string) {
    setModalOpen(false);
    await refreshConnections();
    setActiveConnectionId(connectionId);
  }

  async function send(text?: string) {
    const q = (text ?? question).trim();
    if (!q || busy) return;
    setQuestion("");
    setBusy(true);

    const userMsg: ChatMessage = { id: uid(), role: "user", text: q, createdAt: Date.now() };
    setMessages((m) => [...m, userMsg]);
    setHistory((h) => [q, ...h]);

    try {
      const res: UADAResponse = await postQuery(q, sessionId, activeConnectionId);
      setSessionId(res.session_id);
      setMessages((m) => [
        ...m,
        { id: uid(), role: "assistant", text: "", createdAt: Date.now(), response: res },
      ]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        {
          id: uid(),
          role: "assistant",
          text: "",
          createdAt: Date.now(),
          errorText: e instanceof ApiError ? e.message : "Request failed. Is the backend running?",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const lastSql = lastAssistant?.response?.sql ?? null;
  const lastResponse = lastAssistant?.response ?? null;
  const activeConn = connections.find((c) => c.connection_id === activeConnectionId) ?? null;

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar
        connections={connections}
        activeConnectionId={activeConnectionId}
        onSelect={selectConnection}
        onAddClick={() => setModalOpen(true)}
        history={history}
      />

      <main className="flex flex-1 flex-col overflow-hidden bg-bg">
        <div className="flex h-[60px] flex-shrink-0 items-center gap-3 border-b border-border bg-surface px-6">
          <div className="text-sm">
            <span className="text-muted">Connections</span>
            <span className="mx-1.5 text-border">/</span>
            <span className="font-semibold text-text">{activeConn?.name ?? "No connection"}</span>
          </div>
          <div className="ml-auto flex items-center gap-2 text-xs text-muted">
            {busy && (
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-border border-t-accent" />
            )}
            <span>{busy ? "Thinking…" : ""}</span>
          </div>
        </div>

        <div ref={chatRef} className="flex flex-1 flex-col gap-4 overflow-y-auto p-6">
          {messages.length === 0 && (
            <div className="py-16 text-center">
              <div className="text-3xl font-bold tracking-tight text-text">
                Ask anything about your data
              </div>
              <div className="mt-2.5 text-sm text-muted">
                Connect a database and start a conversation
              </div>
            </div>
          )}
          {messages.map((m) => (
            <MessageBubble key={m.id} msg={m} onPickQuestion={(q) => send(q)} />
          ))}
        </div>

        <div className="flex-shrink-0 border-t border-border bg-surface px-6 py-4">
          <div className="flex items-end gap-2.5">
            <textarea
              rows={1}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder="Ask a question about your data…"
              className="max-h-[120px] min-h-[44px] flex-1 resize-none rounded-lg border border-border bg-surface-2 px-3.5 py-2.5 text-sm leading-relaxed text-text outline-none transition-colors focus:border-accent focus:bg-surface"
            />
            <button
              onClick={() => send()}
              disabled={busy || !question.trim()}
              className="h-[44px] flex-shrink-0 rounded-lg bg-accent px-[18px] text-sm font-semibold text-accent-fg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Send
            </button>
          </div>
        </div>
      </main>

      <RightPanel
        sql={lastSql}
        connectionId={activeConnectionId}
        discovery={discovery}
        lastResponse={lastResponse}
      />

      {modalOpen && (
        <ConnectionModal onClose={() => setModalOpen(false)} onCreated={handleCreated} />
      )}
    </div>
  );
}
