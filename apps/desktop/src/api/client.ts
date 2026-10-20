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
  REP_AUDIO_NOT_READY:
    "Check audio before calling. Reconnect your headset or microphone, then start the call.",
  TELEPHONY_CREDENTIAL_MISCONFIGURED:
    "Telnyx credential needs SIP URI calling set to internal, see Settings.",
};
type Detail = { code: string; message?: string; reasons?: string[] };
function refusal(detail: unknown, status: number) {
  if (typeof detail === "string") return new ApiError(detail, status);
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
export async function bindAuthority(
  runtime: Runtime,
  session: Session,
  local: boolean,
) {
  if (native())
    await invoke("bind_authority", {
      binding: {
        url: runtime.url,
        token: session.access_token,
        tenant_id: session.tenant_id,
        local,
        simulation: runtime.simulation,
      },
    });
}
export async function protectedProviderInput(
  field: ProviderCredential,
  version: number,
) {
  if (!native())
    throw new Error(
      "Enter provider credentials in the installed desktop's protected dialog.",
    );
  return await invoke<{ version: number; configured: Record<string, boolean> }>(
    "protected_provider_input",
    { field, expectedVersion: version },
  );
}
export async function disconnectNativeAuthority() {
  await invoke("disconnect_authority");
}
export async function assertNoPendingOptouts(
  tenantId: string,
  simulation: boolean,
) {
  const raw = native()
    ? await readCache(`optouts-${tenantId}`, simulation)
    : localStorage.getItem(`opendialer.optouts.${tenantId}`);
  if (raw === null) return;
  let pending: unknown;
  try {
    pending = JSON.parse(raw);
  } catch {
    throw new Error("Recover the damaged opt-out journal before continuing.");
  }
  if (!Array.isArray(pending))
    throw new Error("Recover the damaged opt-out journal before continuing.");
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
