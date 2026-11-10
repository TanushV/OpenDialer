import { afterEach, describe, expect, it, vi } from "vitest";

/** Minimal stand-ins for the browser's audio engine and sockets. */
class FakeContext {
  static made: FakeContext[] = [];
  state: "suspended" | "running" | "closed" = "suspended";
  closed = 0;
  unlocked = false;
  audioWorklet = { addModule: vi.fn(async () => undefined) };
  destination = {};
  constructor() {
    FakeContext.made.push(this);
  }
  async resume() {
    // WebKit only starts an engine that a click unlocked.
    if (this.unlocked) this.state = "running";
  }
  async close() {
    this.closed++;
    this.state = "closed";
  }
  createMediaStreamSource() {
    return {
      connect: () => ({ connect: () => ({ connect: () => {} }) }),
      disconnect: () => {},
    };
  }
  createGain() {
    return { gain: { value: 1 }, connect: () => {}, disconnect: () => {} };
  }
}
class FakeSocket {
  static OPEN = 1;
  static made: FakeSocket[] = [];
  readyState = 1;
  bufferedAmount = 0;
  onopen?: () => void;
  onmessage?: (event: { data: string }) => void;
  onclose?: () => void;
  onerror?: () => void;
  constructor() {
    FakeSocket.made.push(this);
  }
  send() {}
  close() {}
}

async function load() {
  vi.resetModules();
  vi.doMock("../src/telephony/pcm-worklet.ts?worker&url", () => ({
    default: "worklet.js",
  }));
  return import("../src/telephony/speech");
}

describe("live speech capture uses one engine the rep's click unlocked", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeContext.made = [];
    FakeSocket.made = [];
  });

  it("reuses the unlocked engine for every call and never closes it", async () => {
    vi.stubGlobal("AudioContext", FakeContext);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal(
      "AudioWorkletNode",
      class {
        port = { onmessage: null };
        connect() {
          return { connect: () => {} };
        }
        disconnect() {}
      },
    );
    const speech = await load();
    speech.unlockSpeechAudio();
    FakeContext.made[0].unlocked = true;
    const streams = { rep: {} as MediaStream, remote: {} as MediaStream };
    const failed = vi.fn();
    for (let call = 0; call < 2; call++) {
      const capture = speech.captureSpeech(
        "http://127.0.0.1:1",
        "t",
        `c${call}`,
        streams,
        failed,
      );
      for (const socket of FakeSocket.made.slice(-2))
        socket.onmessage?.({ data: JSON.stringify({ status: "ready" }) });
      await new Promise((r) => setTimeout(r, 0));
      capture.stop();
    }
    expect(FakeContext.made).toHaveLength(1);
    expect(FakeContext.made[0].closed).toBe(0);
    expect(FakeContext.made[0].audioWorklet.addModule).toHaveBeenCalledTimes(1);
    expect(failed).not.toHaveBeenCalled();
  });

  it("says the engine is suspended when nothing unlocked it", async () => {
    vi.stubGlobal("AudioContext", FakeContext);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal(
      "AudioWorkletNode",
      class {
        port = { onmessage: null };
        connect() {
          return { connect: () => {} };
        }
        disconnect() {}
      },
    );
    const speech = await load();
    const failed = vi.fn();
    speech.captureSpeech(
      "http://127.0.0.1:1",
      "t",
      "c",
      { rep: {} as MediaStream, remote: {} as MediaStream },
      failed,
    );
    FakeSocket.made[0].onmessage?.({
      data: JSON.stringify({ status: "ready" }),
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    expect(failed).toHaveBeenCalledWith("audio_suspended");
  });
});
