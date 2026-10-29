import { createReceiver } from "./receiver.ts";
const url = Deno.env.get("SUPABASE_URL")!;
const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
async function rpc(name: string, body: unknown) {
  const response = await fetch(`${url}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error("relay_storage_unavailable");
  return await response.json();
}
Deno.serve(
  createReceiver({
    now: Date.now,
    route: (connection, call_identity) =>
      rpc("relay_route", { connection, call_identity }),
    persist: (receipt) => rpc("relay_receive", { receipt }),
  }),
);
