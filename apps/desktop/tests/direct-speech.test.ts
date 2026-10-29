import { expect, test } from "vitest";
import { PCM } from "../src/telephony/pcm";

test.each([16000, 44100, 48000])("PCM keeps real duration at %i Hz", (rate) => {
  const packets: Int16Array[] = [];
  const pcm = new PCM(rate, (packet) => packets.push(packet));
  for (let i = 0; i < rate; i += 128)
    pcm.push(new Float32Array(Math.min(128, rate - i)).fill(0.5));
  expect(packets.length).toBe(10);
  expect(packets[0].length).toBe(1600);
  expect(packets[0][0]).toBe(16383);
});
test("silence stays silent and samples are clipped", () => {
  const packets: Int16Array[] = [];
  const pcm = new PCM(16000, (packet) => packets.push(packet));
  pcm.push(new Float32Array(1600));
  pcm.push(new Float32Array(1600).fill(-2));
  expect(packets[0].every((x) => x === 0)).toBe(true);
  expect(packets[1].every((x) => x === -32768)).toBe(true);
});
