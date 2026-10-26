import { invoke } from "@tauri-apps/api/core";
import type { Runtime, Session } from "../types";
import { persistentCache } from "../state/privacy";
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    /** The authority's stable refusal code, when it sent one. */
    public readonly code?: string,
  ) {
    super(message);
  }
}
/** Coded refusals the rep or administrator can act on directly. */
const ACTIONABLE: Record<string, string> = {
  TELNYX_NOT_CONFIGURED:
    "Connect Telnyx in Settings → Setup → Calling before using numbers or calls.",
  REP_AUDIO_NOT_READY:
    "Check audio before calling. Reconnect your headset or microphone, then start the call.",
  TELEPHONY_CREDENTIAL_MISCONFIGURED:
    "Telnyx credential needs SIP URI calling set to internal, see Settings.",
};
type Detail = { code: string; message?: string; reasons?: string[] };
function refusal(detail: unknown, status: number) {
  if (typeof detail === "string")
    return new ApiError(ACTIONABLE[detail] ?? detail, status);
  if (Array.isArray(detail)) {
    const fields = detail
      .filter((item) => item && typeof item.msg === "string")
      .map(
        (item) =>
          `${Array.isArray(item.loc) ? item.loc.slice(1).join(" ").replaceAll("_", " ") : "Field"}: ${item.msg}`,
      );
    if (fields.length) return new ApiError(fields.join("; "), status);
  }
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const { code, message, reasons } = detail as Detail;
    return new ApiError(
      ACTIONABLE[code] ?? message ?? `${code}: ${reasons?.join(", ") ?? ""}`,
      status,
      code,
    );
  }
  return new ApiError(`Request failed (${status})`, status);
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
        canUseOfflineCache(error) ||
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
export type ProviderCredential =
  | "telnyx_api_key"
  | "openrouter_api_key"
  | "openai_api_key"
  | "dnc_api_key";
export function assertNoPendingOptouts(tenantId: string) {
  const raw = localStorage.getItem(`opendialer.optouts.${tenantId}`);
  if (raw === null) return;
  let pending: unknown;
  try {
    pending = JSON.parse(raw);
  } catch {
    throw new Error("Recover the damaged opt-out queue before continuing.");
  }
  if (!Array.isArray(pending))
    throw new Error("Recover the damaged opt-out queue before continuing.");
  if (pending.length)
    throw new Error(
      "Synchronize pending opt-outs before updating or changing workspace.",
    );
}
export interface TunnelConfig {
  enabled: boolean;
  host: string;
  user: string;
  remote_port: number;
}
export interface TunnelStatus {
  config: TunnelConfig;
  state: string;
}
export async function tunnelStatus(): Promise<TunnelStatus> {
  return await invoke("webhook_tunnel_status");
}
export async function saveTunnel(config: TunnelConfig) {
  await invoke("webhook_tunnel_set", { config });
}
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
/** An unanswered authority request is an offline result, never an endless wait. */
export const REQUEST_TIMEOUT_MS = 30_000;
/** Lead/registry imports, statement imports and backup/restore legitimately run longer. */
export const LONG_REQUEST_MS = 300_000;
export interface RequestOptions {
  timeoutMs?: number;
}
export async function request<T>(
  url: string,
  token: string | null,
  path: string,
  method = "GET",
  body?: unknown,
  extraHeaders?: Record<string, string>,
  options: RequestOptions = {},
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? REQUEST_TIMEOUT_MS,
  );
  let response: Response;
  let result: T & { detail?: string | { code: string; reasons?: string[] } };
  try {
    response = await fetch(url.replace(/\/$/, "") + path, {
      method,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...extraHeaders,
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    result = await response.json();
  } catch (error) {
    if (controller.signal.aborted)
      throw new DOMException(
        "The workspace authority did not respond in time.",
        "AbortError",
      );
    throw error;
  } finally {
    clearTimeout(timer);
  }
  if (!response.ok) throw refusal(result.detail, response.status);
  return result;
}
export interface SavedSession {
  connection: "local" | "connected";
  simulation: boolean;
  url?: string;
  session?: Session;
}
const SESSION_KEY = "opendialer.session";
export function saveSession(data: SavedSession) {
  localStorage.setItem(SESSION_KEY, JSON.stringify(data));
}
export function loadSession(): SavedSession | null {
  const value = localStorage.getItem(SESSION_KEY);
  return value ? (JSON.parse(value) as SavedSession) : null;
}
export function clearSession() {
  localStorage.removeItem(SESSION_KEY);
}
export async function openSource(url: string) {
  if (!url.startsWith("https://")) return;
  if (native()) await invoke("open_source", { value: url });
  else window.open(url, "_blank", "noopener,noreferrer");
}
export function validateServer(url: string) {
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
/** Offline viewing: the last copy of a read, without raw speech, kept in local storage. */
export function readCache(namespace: string): string | null {
  return localStorage.getItem("opendialer.cache." + namespace);
}
export function writeCache(namespace: string, value: unknown) {
  try {
    localStorage.setItem(
      "opendialer.cache." + namespace,
      JSON.stringify(persistentCache(value)),
    );
  } catch {
    /* A full store only loses the offline copy. */
  }
}
