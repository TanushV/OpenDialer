import { test, expect } from "./fixtures";

test("direct capture sends two bounded PCM tracks and releases only its own resources", async ({
  page,
}) => {
  const packets: Buffer[] = [];
  let stops = 0;
  await page.routeWebSocket("**/v1/calls/test-audio/audio/*", (socket) => {
    socket.onMessage((message) => {
      if (typeof message === "string") {
        if (JSON.parse(message).token)
          socket.send(JSON.stringify({ status: "ready" }));
        if (JSON.parse(message).stop) {
          stops++;
          socket.send(JSON.stringify({ status: "stopped" }));
        }
      } else packets.push(message);
    });
  });
  await page.goto("/");
  await page.locator("body").click({ position: { x: 1, y: 1 } });
  const tracksAlive = await page.evaluate(async () => {
    // Synthetic browser audio only. No microphone, carrier, cloud or audio files.
    const { captureSpeech } = await import(
      "/src/telephony/speech.ts" as string
    );
    const context = new AudioContext();
    const oscillator = context.createOscillator();
    const source = context.createMediaStreamDestination();
    oscillator.connect(source);
    oscillator.start();
    await context.resume();
    const captured = captureSpeech(
      location.origin,
      "synthetic-session",
      "test-audio",
      { rep: source.stream, remote: source.stream },
      () => {
        throw Error("capture failed");
      },
    );
    await new Promise((resolve) => setTimeout(resolve, 1200));
    captured.stop();
    const alive = source.stream
      .getTracks()
      .every((t) => t.readyState === "live");
    oscillator.stop();
    await context.close();
    return alive;
  });
  expect(tracksAlive).toBe(true);
  await expect.poll(() => stops).toBe(2);
  expect(packets.length).toBeGreaterThan(4);
  expect(packets.every((p) => p.length === 3200)).toBe(true);
  expect(packets.some((p) => p.some((value) => value !== 0))).toBe(true);
});
