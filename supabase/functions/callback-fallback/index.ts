import { createWorker, telnyxActions } from "./worker.ts";

const url = Deno.env.get("SUPABASE_URL")!;
const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const token = Deno.env.get("OPENDIALER_CALLBACK_WORKER_TOKEN") ?? "";
const carrier = Deno.env.get("OPENDIALER_TELNYX_CALLBACK_KEY") ?? "";
const account = Deno.env.get("OPENDIALER_TELNYX_ACCOUNT_ID") ?? "";
async function rpc(name: string, body: unknown) {
  const response = await fetch(url + "/rest/v1/rpc/" + name, {
    method: "POST",
    headers: { apikey: key, Authorization: "Bearer " + key, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(4000),
  });
  if (!response.ok) throw Error("callback_storage_unavailable");
  if (response.status === 204) return null;
  return await response.json();
}
Deno.serve(createWorker({
  token: token.length >= 32 && carrier && account ? token : "",
  take: () => rpc("relay_callback_take", { batch_size: 6 }),
  run: telnyxActions(carrier, account),
  finish: (job, result) => rpc("relay_callback_finish", { job, result }),
}));
