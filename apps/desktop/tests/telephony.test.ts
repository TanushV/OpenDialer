import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Telephony } from "../src/telephony/client";

const sdk = vi.hoisted(() => ({ clients: [] as any[] }));
vi.mock("@telnyx/webrtc", () => ({
  TelnyxRTC: class {
    handlers = new Map<string, (event?: any) => void>();
    disconnect = vi.fn();
    constructor() {
      sdk.clients.push(this);
    }
    on(name: string, handler: (event?: any) => void) {
      this.handlers.set(name, handler);
    }
    connect() {
      this.handlers.get("telnyx.ready")?.();
    }
    emit(call: any) {
      this.handlers.get("telnyx.notification")?.({ type: "callUpdate", call });
    }
  },
}));

function call(id: string, state = "ringing", providerId = id) {
  return {
    id,
    state,
    direction: "inbound",
    telnyxIDs: { telnyxCallControlId: providerId },
    answer: vi.fn(async () => {}),
    hangup: vi.fn(async () => {}),
    toggleAudioMute: vi.fn(),
  };
}
async function connected() {
  const phone = new Telephony();
  await phone.connect("synthetic-token", {} as HTMLAudioElement);
  return { phone, client: sdk.clients.at(-1) };
}
beforeEach(() => {
  sdk.clients.length = 0;
  vi.stubGlobal("navigator", {
    mediaDevices: {
      getUserMedia: async () => ({
        getTracks: () => [{ stop: vi.fn() }],
      }),
    },
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("auto-answer requires the exact authority-confirmed agent identity", async () => {
  const { phone, client } = await connected();
  phone.expectAgentLeg("verified-agent");
  const stranger = call("unexpected");
  client.emit(stranger);
  expect(stranger.answer).not.toHaveBeenCalled();
  const agent = call("sdk-agent", "ringing", "verified-agent");
  client.emit(agent);
  client.emit(agent);
  await Promise.resolve();
  expect(agent.answer).toHaveBeenCalledTimes(1);
});

test("agent ringing before the HTTP receipt is held until that identity arrives", async () => {
  const { phone, client } = await connected();
  const agent = call("sdk-agent", "ringing", "verified-agent");
  client.emit(agent);
  expect(agent.answer).not.toHaveBeenCalled();
  phone.expectAgentLeg("verified-agent");
  await Promise.resolve();
  expect(agent.answer).toHaveBeenCalledTimes(1);
});

test("an old leg terminal event cannot replace or disconnect the active leg", async () => {
  const { phone, client } = await connected();
  phone.expectAgentLeg("old");
  const old = call("old");
  client.emit(old);
  client.emit({ ...old, state: "active" });
  client.emit({ ...old, state: "destroy" });
  phone.expectAgentLeg("new");
  const current = call("new");
  client.emit(current);
  client.emit({ ...current, state: "active" });
  const status = vi.fn();
  phone.onStatus = status;
  client.emit({ ...old, state: "hangup" });
  expect(status).not.toHaveBeenCalledWith("agent-disconnected");
  phone.mute();
  expect(current.toggleAudioMute).toHaveBeenCalledTimes(1);
});

test("disconnected SDK callbacks cannot acquire a new local call", async () => {
  const { phone, client } = await connected();
  phone.expectAgentLeg("old");
  phone.disconnect();
  const old = call("old", "active");
  client.emit(old);
  phone.mute();
  await phone.emergencyHangup();
  expect(old.toggleAudioMute).not.toHaveBeenCalled();
  expect(old.hangup).not.toHaveBeenCalled();
});

test("return calls are accepted explicitly and controls target their SDK leg", async () => {
  const { phone, client } = await connected();
  const inbound = call("sdk-return", "ringing", "provider-return");
  client.emit(inbound);
  expect(inbound.answer).not.toHaveBeenCalled();
  phone.expectReturnLeg("provider-return");
  expect(phone.incoming?.providerId).toBe("provider-return");
  await expect(phone.acceptIncoming("foreign")).rejects.toThrow();
  await phone.acceptIncoming("provider-return");
  await phone.acceptIncoming("provider-return");
  expect(inbound.answer).toHaveBeenCalledTimes(1);
  client.emit({ ...inbound, state: "active" });
  phone.mute("provider-return");
  await phone.emergencyHangup("provider-return");
  expect(inbound.toggleAudioMute).toHaveBeenCalledTimes(1);
  expect(inbound.hangup).toHaveBeenCalledTimes(1);
});

test("a stale return-call control cannot stop a newly active agent", async () => {
  const { phone, client } = await connected();
  phone.expectAgentLeg("new-agent");
  const agent = call("new-agent");
  client.emit(agent);
  client.emit({ ...agent, state: "active" });
  phone.mute("old-return");
  await phone.emergencyHangup("old-return");
  expect(agent.hangup).not.toHaveBeenCalled();
  expect(agent.toggleAudioMute).not.toHaveBeenCalled();
});

test("unverified active notifications never acquire local controls", async () => {
  const { phone, client } = await connected();
  const foreign = call("foreign", "active");
  client.emit(foreign);
  phone.mute();
  await phone.emergencyHangup();
  expect(foreign.toggleAudioMute).not.toHaveBeenCalled();
  expect(foreign.hangup).not.toHaveBeenCalled();
});

test("disconnect while microphone permission is pending cannot register later", async () => {
  let release!: (value: any) => void;
  navigator.mediaDevices.getUserMedia = vi.fn(
    () =>
      new Promise((resolve) => {
        release = resolve;
      }),
  );
  const phone = new Telephony();
  const pending = phone.connect("synthetic-token", {} as HTMLAudioElement);
  phone.disconnect();
  const stop = vi.fn();
  release({ getTracks: () => [{ stop }] });
  await expect(pending).rejects.toThrow();
  expect(stop).toHaveBeenCalledTimes(1);
  expect(sdk.clients).toHaveLength(0);
});

test("reusing a ready registration does not leave a second SDK client", async () => {
  const { phone } = await connected();
  await phone.connect("new-short-lived-token", {} as HTMLAudioElement);
  expect(sdk.clients).toHaveLength(1);
});

test("ending a callback preserves agent ownership and rejects a late callback active event", async () => {
  const { phone, client } = await connected();
  phone.expectAgentLeg("agent");
  const agent = call("agent");
  client.emit(agent);
  client.emit({ ...agent, state: "active" });
  const incoming = call("incoming");
  client.emit(incoming);
  phone.expectReturnLeg("incoming");
  await phone.acceptIncoming("incoming");
  client.emit({ ...incoming, state: "active" });
  await phone.emergencyHangup("incoming");
  client.emit({ ...incoming, state: "active" });
  phone.mute();
  expect(agent.toggleAudioMute).toHaveBeenCalledTimes(1);
  expect(incoming.toggleAudioMute).not.toHaveBeenCalled();
  expect(agent.hangup).not.toHaveBeenCalled();
});
