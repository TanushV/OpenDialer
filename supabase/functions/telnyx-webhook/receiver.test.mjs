import { test } from "node:test";
import assert from "node:assert/strict";
import { createReceiver } from "./receiver.ts";
const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, [
  "sign",
  "verify",
]);
const publicKey = Buffer.from(
  await crypto.subtle.exportKey("raw", pair.publicKey),
).toString("base64");
const now = 1800000000000;
const event = {
  id: "evt-1",
  event_type: "call.hangup",
  occurred_at: new Date(now).toISOString(),
  payload: {
    connection_id: "app-1",
    call_control_id: "call-1",
    hangup_cause: "normal_clearing",
    client_state: Buffer.from(
      JSON.stringify({ call_id: "local-1", secret: "remove" }),
    ).toString("base64"),
    text: "never store this",
  },
};
async function request(
  data = event,
  stamp = Math.floor(now / 1000),
  tamper = false,
) {
  const body = JSON.stringify({ data });
  const signature = Buffer.from(
    await crypto.subtle.sign(
      "Ed25519",
      pair.privateKey,
      new TextEncoder().encode(`${stamp}|${body}`),
    ),
  ).toString("base64");
  return new Request("https://relay.test/telnyx-webhook", {
    method: "POST",
    headers: {
      "telnyx-timestamp": String(stamp),
      "telnyx-signature-ed25519": signature,
    },
    body: tamper ? body + " " : body,
  });
}
function harness() {
  const saved = [];
  const receiver = createReceiver({
    now: () => now,
    route: async (id) =>
      id === "app-1"
        ? {
            workspace_id: "owner-1",
            public_key: publicKey,
            account_id: "account-1",
          }
        : null,
    persist: async (item) => {
      const old = saved.find((x) => x.event.id === item.event.id);
      if (old && old.hash !== item.hash) return "collision";
      if (!old) saved.push(item);
      return old ? "duplicate" : "received";
    },
  });
  return { saved, receiver };
}
test("valid event is routed, sanitized and deduplicated", async () => {
  const { saved, receiver } = harness();
  assert.equal((await receiver(await request())).status, 200);
  assert.equal((await receiver(await request())).status, 200);
  assert.equal(saved.length, 1);
  assert.equal(saved[0].workspace_id, "owner-1");
  assert.equal(saved[0].event.payload.text, undefined);
  assert.equal(
    JSON.parse(Buffer.from(saved[0].event.payload.client_state, "base64"))
      .secret,
    undefined,
  );
});
test("signature, timestamp and route rejection never persists", async () => {
  const { saved, receiver } = harness();
  assert.equal(
    (await receiver(await request(event, Math.floor(now / 1000), true))).status,
    401,
  );
  assert.equal(
    (await receiver(await request(event, Math.floor(now / 1000) - 301))).status,
    401,
  );
  assert.equal(
    (
      await receiver(
        await request({
          ...event,
          payload: { ...event.payload, connection_id: "unknown" },
        }),
      )
    ).status,
    404,
  );
  assert.equal(saved.length, 0);
});
test("same identity with changed signed body conflicts", async () => {
  const { receiver } = harness();
  await receiver(await request());
  assert.equal(
    (
      await receiver(
        await request({
          ...event,
          payload: { ...event.payload, hangup_cause: "changed" },
        }),
      )
    ).status,
    409,
  );
});
test("unexpected speech is acknowledged without saving or returning text", async () => {
  const { receiver, saved } = harness();
  const response = await receiver(
    await request({
      ...event,
      event_type: "call.transcription",
      payload: {
        ...event.payload,
        transcription_data: { transcript: "private speech" },
      },
    }),
  );
  assert.equal(response.status, 200);
  assert.equal(saved.length, 1);
  assert.equal(saved[0].event.event_type, "relay.unexpected_transcription");
  assert.ok(!JSON.stringify(saved).includes("private speech"));
  assert.ok(!(await response.text()).includes("private speech"));
});
test("invalid JSON and oversized body are bounded", async () => {
  const { receiver } = harness();
  assert.equal(
    (
      await receiver(
        new Request("https://relay.test", { method: "POST", body: "{" }),
      )
    ).status,
    400,
  );
  assert.equal(
    (
      await receiver(
        new Request("https://relay.test", {
          method: "POST",
          body: "x".repeat(131073),
        }),
      )
    ).status,
    413,
  );
});

test("malformed controls cannot poison the local pending queue", async () => {
  const { receiver, saved } = harness();
  for (const bad of [
    { ...event, event_type: "" },
    { ...event, payload: {} },
    { ...event, occurred_at: "not-a-time" },
  ]) {
    assert.equal((await receiver(await request(bad))).status, 400);
  }
  assert.equal(saved.length, 0);
});
