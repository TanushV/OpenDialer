import { describe, expect, it, vi } from "vitest";
import { newestGate, readNewest } from "../src/state/newest";

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("overlapping re-reads", () => {
  // CI, November 10: Pause, Resume and Remove each re-read the lists, and so did the event each one caused. The
  // read made after Pause answered last and put the removed list back on screen as Paused.
  it("never lets an older answer replace a newer one", async () => {
    const gate = newestGate();
    const older = pending<string>();
    const newer = pending<string>();
    const show = vi.fn();
    const first = readNewest(gate, () => older.promise, show);
    const second = readNewest(gate, () => newer.promise, show);
    newer.resolve("removed");
    await second;
    older.resolve("paused");
    await first;
    expect(show.mock.calls).toEqual([["removed"]]);
  });

  it("shows answers that arrive in order, and a failed read still throws", async () => {
    const gate = newestGate();
    const show = vi.fn();
    await readNewest(gate, async () => "paused", show);
    await readNewest(gate, async () => "active", show);
    await expect(
      readNewest(gate, () => Promise.reject(new Error("offline")), show),
    ).rejects.toThrow("offline");
    expect(show.mock.calls).toEqual([["paused"], ["active"]]);
  });
});
