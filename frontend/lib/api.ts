import type {
  ConnectionCreatedResponse,
  ConnectionCreateRequest,
  ConnectionSummary,
  DiscoverResponse,
  SchemaRefreshResponse,
  TestConnectionResult,
  UADAResponse,
} from "./types";

const BASE = "/api/backend";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body.message || JSON.stringify(body);
    } catch {
      /* response wasn't JSON */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ── Connections ────────────────────────────────────────────────────────────

export function listConnections(): Promise<{ connections: ConnectionSummary[] }> {
  return request("/connections");
}

export function createConnection(
  body: ConnectionCreateRequest,
): Promise<ConnectionCreatedResponse> {
  return request("/connections", { method: "POST", body: JSON.stringify(body) });
}

export function deleteConnection(connectionId: string): Promise<void> {
  return request(`/connections/${connectionId}`, { method: "DELETE" });
}

export function testConnection(connectionId: string): Promise<TestConnectionResult> {
  return request(`/connections/${connectionId}/test`, { method: "POST" });
}

export function discoverSchema(connectionId: string): Promise<DiscoverResponse> {
  return request(`/connections/${connectionId}/discover`, { method: "POST" });
}

export function browseSchema(connectionId: string): Promise<SchemaRefreshResponse> {
  return request(`/connections/${connectionId}/schema`);
}

// ── Query / session ──────────────────────────────────────────────────────────

export function postQuery(
  question: string,
  sessionId: string | null,
  connectionId: string | null,
): Promise<UADAResponse> {
  return request("/query", {
    method: "POST",
    body: JSON.stringify({ question, session_id: sessionId, connection_id: connectionId }),
  });
}

export function deleteSession(sessionId: string): Promise<{ deleted: boolean }> {
  return request(`/session/${sessionId}`, { method: "DELETE" });
}

export { ApiError };
