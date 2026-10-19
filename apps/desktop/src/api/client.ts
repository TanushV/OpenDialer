import { invoke } from "@tauri-apps/api/core";
import type { Runtime, Session } from "../types";
import { persistentCache } from "../state/privacy";
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}
// Only local, versioned business edits may replay after an uncertain transport result.
// Telephony and charged provider actions retain their operation-specific recovery.
export async function localEdit<T>(
  api: <R>(path: string, method?: string, body?: unknown) => Promise<R>,
  path: string,
  method: string,
  body: {
    request_id: string;
    expected_version: number;
    [key: string]: unknown;
  },
): Promise<T> {
  const allowed =
    (method === "PATCH" && /^\/v1\/tasks\/[^/]+$/.test(path)) ||
    (method === "POST" && /^\/v1\/calls\/[^/]+\/disposition$/.test(path)) ||
    (method === "POST" && /^\/v1\/leads\/[^/]+\/meeting$/.test(path)) ||
    (method === "PUT" && path === "/v1/admin/operating-policy") ||
    (method === "PATCH" && /^\/v1\/leads\/[^/]+\/outreach$/.test(path));
  if (!allowed) throw new Error("This route is not an identified local edit");
  const original = structuredClone(body);
  try {
    return await api<T>(path, method, original);
  } catch (error) {
    if (
      !(
        error instanceof TypeError ||
        error instanceof SyntaxError ||
        (error instanceof ApiError && error.status >= 500)
      )
    )
      throw error;
    return await api<T>(path, method, original);
  }
}
export function canUseOfflineCache(error: unknown) {
  return (
    error instanceof TypeError ||
    (error instanceof DOMException && error.name === "AbortError")
  );
}
export const native = () => "__TAURI_INTERNALS__" in window;
export async function localRuntime(simulation: boolean): Promise<Runtime> {
  if (native()) return await invoke("backend_config", { simulation });
  if (!import.meta.env.DEV)
    throw new Error(
      "Standalone mode requires the installed desktop application.",
    );
  return {
    url: import.meta.env.VITE_BACKEND_URL || "http://127.0.0.1:8765",
    local_token: import.meta.env.VITE_LOCAL_TOKEN || "",
    simulation,
  };
}
export async function waitForRuntime(runtime: Runtime) {
  for (let attempt = 0; attempt < 120; attempt++) {
    try {
      const r = await fetch(runtime.url + "/health");
      if (r.ok) return (await r.json()) as { initialized: boolean };
    } catch {
      /* sidecar starting */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(
    "The bundled local service did not start. Open Settings to review diagnostics.",
  );
}
export async function request<T>(
  url: string,
  token: string | null,
  path: string,
  method = "GET",
  body?: unknown,
  extraHeaders?: Record<string, string>,
): Promise<T> {
  const response = await fetch(url.replace(/\/$/, "") + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...extraHeaders,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = (await response.json()) as T & {
    detail?: string | { code: string; reasons?: string[] };
  };
  if (!response.ok) {
    const detail = result.detail;
    throw new ApiError(
      typeof detail === "string"
        ? detail
        : detail && !Array.isArray(detail)
          ? `${detail.code}: ${detail.reasons?.join(", ") ?? ""}`
          : `Request failed (${response.status})`,
      response.status,
    );
  }
  return result;
}
export interface SavedSession {
  connection: "local" | "connected";
  simulation: boolean;
  url?: string;
  session?: Session;
}
export async function saveSession(data: SavedSession) {
  const value = JSON.stringify(data);
  if (native()) {
    if (data.connection === "connected") {
      await invoke("save_session", { value });
      localStorage.setItem("opendialer.mode", "connected");
    } else localStorage.setItem("opendialer.mode", value);
  } else sessionStorage.setItem("opendialer.session", value);
}
export async function loadSession(): Promise<SavedSession | null> {
  if (native()) {
    const mode = localStorage.getItem("opendialer.mode");
    if (!mode) return null;
    if (mode !== "connected") return JSON.parse(mode) as SavedSession;
    const value = await invoke<string | null>("load_session");
    return value ? (JSON.parse(value) as SavedSession) : null;
  }
  const value = sessionStorage.getItem("opendialer.session");
  return value ? (JSON.parse(value) as SavedSession) : null;
}
export async function clearSession() {
  if (native()) {
    if (localStorage.getItem("opendialer.mode") === "connected")
      await invoke("clear_session");
    localStorage.removeItem("opendialer.mode");
  } else sessionStorage.removeItem("opendialer.session");
}
export async function openSource(url: string) {
  if (!url.startsWith("https://")) return;
  if (native()) await invoke("open_source", { value: url });
  else window.open(url, "_blank", "noopener,noreferrer");
}
export async function validateServer(url: string) {
  if (native()) return await invoke("check_server_url", { value: url });
  const parsed = new URL(url);
  if (
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    (parsed.protocol !== "https:" &&
      !(
        parsed.protocol === "http:" &&
        ["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname)
      ))
  )
    throw new Error(
      "Use HTTPS for your server, or a loopback HTTP development server.",
    );
}
export async function readCache(
  namespace: string,
  simulation: boolean,
): Promise<string | null> {
  if (!native()) return null;
  return await invoke("cache_read", {
    namespace: namespace.startsWith("optouts-")
      ? namespace
      : "safe-v1-" + namespace,
    simulation,
  });
}
export async function writeCache(
  namespace: string,
  value: unknown,
  simulation: boolean,
): Promise<void> {
  if (native())
    await invoke("cache_write", {
      namespace: namespace.startsWith("optouts-")
        ? namespace
        : "safe-v1-" + namespace,
      value: JSON.stringify(persistentCache(value)),
      simulation,
    });
}
