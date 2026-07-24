import { invoke } from "@tauri-apps/api/core";

export interface EngineConnection {
  baseUrl: string;
  token: string;
}

let connection: Promise<EngineConnection | null> | null = null;

function isDesktop(): boolean {
  return "__TAURI_INTERNALS__" in window;
}

async function engineConnection(): Promise<EngineConnection | null> {
  if (!isDesktop()) return null;
  connection ??= invoke<EngineConnection>("engine_connection");
  return connection;
}

export function resolveApiUrl(input: RequestInfo | URL, engine: EngineConnection | null): RequestInfo | URL {
  if (!engine || typeof input !== "string" || !input.startsWith("/api/")) return input;
  return `${engine.baseUrl}${input}`;
}

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const engine = await engineConnection();
  const target = resolveApiUrl(input, engine);
  if (!engine || target === input) {
    return window.fetch(input, init);
  }

  const headers = new Headers(init.headers);
  headers.set("X-LogPilot-Token", engine.token);
  return window.fetch(target, { ...init, headers });
}
