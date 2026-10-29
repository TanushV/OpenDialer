// Runtime-independent signed receiver; no provider bodies in logs or error responses.
type Route = { workspace_id: string; account_id: string; public_key: string };
type Receipt = {
  workspace_id: string;
  account_id: string;
  hash: string;
  received_at: string;
  event: Record<string, unknown>;
};
type Dependencies = {
  now: () => number;
  route: (connection: string, call: string) => Promise<Route | null>;
  persist: (receipt: Receipt) => Promise<string>;
};
const fields = new Set([
  "call_control_id",
  "call_leg_id",
  "call_session_id",
  "connection_id",
  "command_id",
  "direction",
  "result",
  "from",
  "to",
  "recording_id",
  "digit",
  "digits",
  "occurred_at",
  "timing_source",
  "hangup_cause",
  "hangup_source",
  "sip_hangup_cause",
]);
const contextFields = new Set([
  "call_id",
  "tenant_id",
  "agent_leg_id",
  "inbound_task",
  "attempt",
]);
const reply = (status: number, result: string) =>
  Response.json({ result }, { status });
function bytes(value: string) {
  return Uint8Array.from(atob(value), (c) => c.charCodeAt(0));
}
function sanitize(payload: Record<string, unknown>) {
  const result: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(payload))
    if (
      fields.has(k) &&
      ["string", "number", "boolean"].includes(typeof v) &&
      String(v).length <= 512
    )
      result[k] = v;
  if (
    typeof payload.client_state === "string" &&
    payload.client_state.length <= 4096
  ) {
    try {
      const context = JSON.parse(atob(payload.client_state));
      const clean = Object.fromEntries(
        Object.entries(context).filter(
          ([k, v]) =>
            contextFields.has(k) &&
            typeof v === "string" &&
            /^[A-Za-z0-9_.:-]{1,128}$/.test(v),
        ),
      );
      if (Object.keys(clean).length)
        result.client_state = btoa(JSON.stringify(clean));
    } catch {
      /* Opaque context is never stored. */
    }
  }
  return result;
}
export function createReceiver(deps: Dependencies) {
  return async (request: Request): Promise<Response> => {
    if (request.method !== "POST") return reply(405, "method_not_allowed");
    try {
      if (Number(request.headers.get("content-length")) > 131072)
        return reply(413, "too_large");
      const reader = request.body?.getReader();
      if (!reader) return reply(400, "invalid_event");
      let total = 0;
      const chunks: Uint8Array[] = [];
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        total += value.length;
        if (total > 131072) {
          await reader.cancel();
          return reply(413, "too_large");
        }
        chunks.push(value);
      }
      const bodyBytes = new Uint8Array(total);
      let offset = 0;
      for (const c of chunks) {
        bodyBytes.set(c, offset);
        offset += c.length;
      }
      let event;
      try {
        event = JSON.parse(
          new TextDecoder("utf-8", { fatal: true }).decode(bodyBytes),
        ).data;
      } catch {
        return reply(400, "invalid_event");
      }
      if (
        !event ||
        typeof event.id !== "string" ||
        !event.id ||
        event.id.length > 128 ||
        typeof event.event_type !== "string" ||
        !event.event_type ||
        event.event_type.length > 128 ||
        !event.payload ||
        typeof event.payload !== "object" ||
        Array.isArray(event.payload) ||
        typeof event.payload.call_control_id !== "string" ||
        !event.payload.call_control_id ||
        event.payload.call_control_id.length > 512 ||
        (event.occurred_at != null &&
          (typeof event.occurred_at !== "string" ||
            event.occurred_at.length > 64 ||
            !/^\d{4}-\d{2}-\d{2}T/.test(event.occurred_at) ||
            !Number.isFinite(Date.parse(event.occurred_at))))
      )
        return reply(400, "invalid_event");
      const timestamp = request.headers.get("telnyx-timestamp") ?? "";
      if (
        !/^\d{10}$/.test(timestamp) ||
        Math.abs(deps.now() / 1000 - Number(timestamp)) > 300
      )
        return reply(401, "invalid_signature");
      const route = await deps.route(
        String(event.payload.connection_id ?? ""),
        String(event.payload.call_control_id ?? ""),
      );
      if (!route) return reply(404, "unknown_route");
      let verified = false;
      try {
        const key = await crypto.subtle.importKey(
          "raw",
          bytes(route.public_key),
          { name: "Ed25519" },
          false,
          ["verify"],
        );
        const prefix = new TextEncoder().encode(timestamp + "|");
        const signed = new Uint8Array(prefix.length + bodyBytes.length);
        signed.set(prefix);
        signed.set(bodyBytes, prefix.length);
        verified = await crypto.subtle.verify(
          "Ed25519",
          key,
          bytes(request.headers.get("telnyx-signature-ed25519") ?? ""),
          signed,
        );
      } catch {
        /* No raw cryptographic error output. */
      }
      if (!verified) return reply(401, "invalid_signature");
      const hash = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", bodyBytes)),
        (x) => x.toString(16).padStart(2, "0"),
      ).join("");
      const speech = event.event_type === "call.transcription";
      const result = await deps.persist({
        workspace_id: route.workspace_id,
        account_id: route.account_id,
        hash,
        received_at: new Date(deps.now()).toISOString(),
        event: {
          id: event.id,
          event_type: speech
            ? "relay.unexpected_transcription"
            : event.event_type,
          occurred_at:
            typeof event.occurred_at === "string"
              ? new Date(event.occurred_at).toISOString()
              : null,
          payload: sanitize(event.payload),
        },
      });
      return reply(result === "collision" ? 409 : 200, result);
    } catch {
      return reply(503, "temporarily_unavailable");
    }
  };
}
