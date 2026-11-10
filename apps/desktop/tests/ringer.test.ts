import { describe, expect, it, vi } from "vitest";
import { createRinger } from "../src/telephony/ringer";

function fakeAudio() {
  const made: { hz: number; started: boolean; stopped: boolean }[] = [];
  const levels: [number, number][] = [];
  const context = {
    currentTime: 10,
    destination: {},
    resume: vi.fn(async () => undefined),
    createGain: () => ({
      gain: {
        value: 0,
        setValueAtTime: (value: number, at: number) => levels.push([value, at]),
      },
      connect: vi.fn(),
      disconnect: vi.fn(),
    }),
    createOscillator: () => {
      const tone = { hz: 0, started: false, stopped: false };
      made.push(tone);
      return {
        frequency: {
          set value(hz: number) {
            tone.hz = hz;
          },
        },
        connect: vi.fn(),
        disconnect: vi.fn(),
        start: () => (tone.started = true),
        stop: () => (tone.stopped = true),
      };
    },
  };
  return { context: context as unknown as AudioContext, made, levels };
}

describe("the callback ring", () => {
  it("rings two tones on a two-on, four-off cadence until stopped", () => {
    const audio = fakeAudio();
    let tick: () => void = () => undefined;
    const cancel = vi.fn();
    const ringer = createRinger(
      () => audio.context,
      (run, ms) => {
        expect(ms).toBe(6000);
        tick = run;
        return cancel;
      },
    );
    ringer.start();
    ringer.start();
    expect(audio.made.map((t) => t.hz)).toEqual([440, 480]);
    expect(audio.levels).toEqual([
      [0.08, 10],
      [0, 12],
    ]);
    tick();
    expect(audio.levels).toHaveLength(4);
    expect(ringer.ringing).toBe(true);
    ringer.stop();
    ringer.stop();
    expect(ringer.ringing).toBe(false);
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(audio.made.every((t) => t.started && t.stopped)).toBe(true);
  });
});
