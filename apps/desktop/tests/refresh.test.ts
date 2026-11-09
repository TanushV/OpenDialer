import { describe, expect, it, vi } from "vitest";
import { coalesceRefresh, shareUnchanged } from "../src/state/refresh";

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("workspace refresh after a mutation", () => {
  it("reruns after an overlapping save and never publishes the stale response", async () => {
    const first = pending<string>();
    const second = pending<string>();
    const load = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const publish = vi.fn();
    const refresh = coalesceRefresh(load, publish);
    const a = refresh();
    const b = refresh();
    const c = refresh();
    expect(load).toHaveBeenCalledTimes(1);
    first.resolve("paused before rep clicked ready");
    await first.promise;
    await Promise.resolve();
    expect(load).toHaveBeenCalledTimes(2);
    expect(publish).not.toHaveBeenCalled();
    second.resolve("ready");
    await Promise.all([a, b, c]);
    expect(publish).toHaveBeenCalledExactlyOnceWith("ready");
  });

  it("releases the in-flight state after failure so a subsequent request can recover", async () => {
    const publish = vi.fn();
    const refresh = coalesceRefresh(
      vi
        .fn()
        .mockRejectedValueOnce(new Error("offline"))
        .mockResolvedValueOnce("ready"),
      publish,
    );
    await expect(refresh()).rejects.toThrow("offline");
    await refresh();
    expect(publish).toHaveBeenCalledExactlyOnceWith("ready");
  });
});

describe("shareUnchanged", () => {
  it("keeps the shown workspace when a re-read brings nothing new", () => {
    const shown = { leads: [{ id: "a" }], event_cursor: 1 };
    expect(
      shareUnchanged(shown, { leads: [{ id: "a" }], event_cursor: 2 }),
    ).toBe(shown);
  });

  it("replaces only what changed, so screens reading the rest do not redraw", () => {
    const shown = {
      leads: [{ id: "a" }],
      numbers: [{ id: "n" }],
      event_cursor: 1,
    };
    const next = shareUnchanged(shown, {
      leads: [{ id: "b" }],
      numbers: [{ id: "n" }],
      event_cursor: 2,
    });
    expect(next).not.toBe(shown);
    expect(next.numbers).toBe(shown.numbers);
    expect(next.leads).toEqual([{ id: "b" }]);
    expect(next.event_cursor).toBe(2);
  });

  it("takes the first read as it is", () => {
    const first = { leads: [] };
    expect(shareUnchanged(null, first)).toBe(first);
  });
});
