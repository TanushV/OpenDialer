type Job = {
  account_id: string;
  call_id: string;
  action: "answer" | "speak" | "hangup";
  command_id: string;
  greeting?: string;
};
type Result = "ok" | "retry" | "ended";
type Services = {
  token: string;
  take: () => Promise<Job[]>;
  run: (job: Job) => Promise<Result>;
  finish: (job: Job, result: Result) => Promise<void>;
};
const encoder = new TextEncoder();
async function sameToken(actual: string, expected: string) {
  if (!expected || !actual || actual.length > 512) return false;
  const [a, b] = await Promise.all(
    [actual, expected].map(async (s) => new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(s)))),
  );
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}
export function createWorker(service: Services) {
  return async (request: Request) => {
    if (request.method !== "POST") return new Response(null, { status: 405 });
    if (!(await sameToken(request.headers.get("x-opendialer-worker-token") ?? "", service.token)))
      return new Response(null, { status: 401 });
    try {
      for (let round = 0; round < 3; round++) {
        const jobs = await service.take();
        if (!jobs.length) break;
        await Promise.all(jobs.map(async (job) => service.finish(job, await service.run(job))));
      }
      return new Response(null, { status: 200 });
    } catch {
      // Leased work becomes due again. Do not log caller identifiers, greetings or provider response bodies.
      return new Response(null, { status: 503 });
    }
  };
}

const fallbackGreeting = "Thanks for calling. No one can take your call right now, and we will follow up. Goodbye.";
export function telnyxActions(key: string, account: string, send: typeof fetch = fetch) {
  const headers = { Authorization: "Bearer " + key, "Content-Type": "application/json" };
  return async (job: Job): Promise<Result> => {
    if (!key || !account || job.account_id !== account) return "retry";
    const url = "https://api.telnyx.com/v2/calls/" + encodeURIComponent(job.call_id);
    try {
      const status = await send(url, { method: "GET", headers, signal: AbortSignal.timeout(3000) });
      if ([404, 422].includes(status.status)) return "ended";
      if (!status.ok) return "retry";
      const alive = (await status.json())?.data?.is_alive;
      if (alive === false) return "ended";
      if (alive !== true) return "retry";
      const body: Record<string, unknown> = { command_id: job.command_id };
      if (job.action === "speak") {
        const greeting = job.greeting?.trim();
        Object.assign(body, {
          payload: greeting || fallbackGreeting,
          payload_type: "text",
          voice: greeting ? "Telnyx.KokoroTTS.af" : "female",
          language: "en-US",
          service_level: greeting ? "premium" : "basic",
          target_legs: "self",
        });
      }
      const response = await send(url + "/actions/" + job.action, {
        method: "POST", headers, body: JSON.stringify(body), signal: AbortSignal.timeout(4500),
      });
      if (response.status === 404) return "ended";
      return response.ok && (await response.json())?.data?.result === "ok" ? "ok" : "retry";
    } catch {
      return "retry";
    }
  };
}
