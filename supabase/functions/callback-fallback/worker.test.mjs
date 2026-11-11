import { test } from "node:test";
import assert from "node:assert/strict";
import { createWorker, telnyxActions } from "./worker.ts";

test("only the scheduler can drain jobs and dead calls never get a command", async () => {
  const job = { account_id: "account", call_id: "v3:original", action: "answer", command_id: "command" };
  const requests = [], results = [];
  const carrier = telnyxActions("test-key", "account", async (url, init) => {
    requests.push({ url, method: init.method });
    return Response.json({ data: { is_alive: false } });
  });
  let takes = 0;
  const worker = createWorker({
    token: "scheduler-key", take: async () => takes++ === 0 ? [job] : [],
    run: carrier, finish: async (_, result) => results.push(result),
  });
  const req = token => new Request("https://relay.example/fallback", {
    method: "POST", headers: { "x-opendialer-worker-token": token },
  });
  assert.equal((await worker(req("wrong"))).status, 401);
  assert.equal(takes, 0);
  assert.equal((await worker(req("scheduler-key"))).status, 200);
  assert.deepEqual(results, ["ended"]);
  assert.deepEqual(requests, [{ url: "https://api.telnyx.com/v2/calls/v3%3Aoriginal", method: "GET" }]);
});

test("status precedes each command; uncertain replies reuse the same carrier ID", async () => {
  let successes = 0;
  const sent = [];
  const carrier = telnyxActions("test-key", "account", async (url, init) => {
    if (init.method === "GET") return Response.json({ data: { is_alive: true } });
    sent.push([url.split("/").at(-1), JSON.parse(init.body)]);
    if (successes++ === 0) throw new Error("simulated lost HTTP response");
    return Response.json({ data: { result: "ok" } });
  });
  const job = { account_id: "account", call_id: "call", action: "answer", command_id: "answer-id" };
  assert.equal(await carrier(job), "retry");
  assert.equal(await carrier(job), "ok");
  assert.equal(await carrier({ ...job, action: "speak", command_id: "speak-id", greeting: "Sorry we missed you." }), "ok");
  assert.equal(await carrier({ ...job, action: "hangup", command_id: "end-id" }), "ok");
  assert.deepEqual(sent.map(([action, body]) => [action, body.command_id]), [
    ["answer", "answer-id"], ["answer", "answer-id"], ["speak", "speak-id"], ["hangup", "end-id"],
  ]);
  assert.equal(sent[2][1].payload, "Sorry we missed you.");
  assert.equal(await carrier({ ...job, account_id: "other-account" }), "retry");
  assert.equal(sent.length, 4);
});
