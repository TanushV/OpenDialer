import { afterEach, describe, expect, it, vi } from "vitest";
import { MICROPHONE_BLOCKED, microphone } from "../src/telephony/client";

function stubMicrophone(getUserMedia: () => Promise<MediaStream>) {
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
}

describe("the microphone request (G10: no endless wait)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("stops waiting on a permission prompt nobody answers and says what to do", async () => {
    stubMicrophone(() => new Promise<MediaStream>(() => {}));
    await expect(microphone(20)).rejects.toThrow(MICROPHONE_BLOCKED);
  });

  it("explains a refused microphone the same way", async () => {
    stubMicrophone(() =>
      Promise.reject(new DOMException("denied", "NotAllowedError")),
    );
    await expect(microphone(1_000)).rejects.toThrow(MICROPHONE_BLOCKED);
  });

  it("hands over the stream when access is allowed", async () => {
    const stream = { getTracks: () => [] } as unknown as MediaStream;
    stubMicrophone(() => Promise.resolve(stream));
    await expect(microphone(1_000)).resolves.toBe(stream);
  });

  it("releases a stream that arrives after the wait", async () => {
    const stop = vi.fn();
    let allow!: (s: MediaStream) => void;
    stubMicrophone(
      () =>
        new Promise<MediaStream>((resolve) => {
          allow = resolve;
        }),
    );
    await expect(microphone(10)).rejects.toThrow(MICROPHONE_BLOCKED);
    allow({ getTracks: () => [{ stop }] } as unknown as MediaStream);
    await new Promise((r) => setTimeout(r, 0));
    expect(stop).toHaveBeenCalled();
  });
});
