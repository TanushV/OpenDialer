import { afterEach, describe, expect, it, vi } from "vitest";
import { canUseOfflineCache, localEdit, request } from "../src/api/client";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("bounded authority requests", () => {
  it("abandons an unanswered request as an offline result", async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            signal = init.signal ?? undefined;
            signal?.addEventListener("abort", () =>
              reject(new DOMException("aborted", "AbortError")),
            );
          }),
      ),
    );
    const pending = request("http://127.0.0.1:1", null, "/v1/workspace");
    const outcome = pending.then(
      () => null,
      (error: unknown) => error,
    );
    await vi.advanceTimersByTimeAsync(30_000);
    const error = await outcome;
    expect(signal?.aborted).toBe(true);
    expect(canUseOfflineCache(error)).toBe(true);
    expect(String((error as Error).message)).toMatch(/did not respond/);
  });

  it("replays a timed-out local edit with the same identity", async () => {
    const api = vi
      .fn()
      .mockRejectedValueOnce(new DOMException("timed out", "AbortError"))
      .mockResolvedValueOnce({ version: 2 });
    await localEdit(api, "/v1/tasks/task-1", "PATCH", {
      request_id: "4e8f3a1c-55d2-4a0e-9a53-4dbdfb1e02a1",
      expected_version: 1,
    });
    expect(api).toHaveBeenCalledTimes(2);
    expect(api.mock.calls[1]).toEqual(api.mock.calls[0]);
  });
});
