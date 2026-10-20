import { afterEach, describe, expect, it, vi } from "vitest";
import {
  LONG_REQUEST_MS,
  REQUEST_TIMEOUT_MS,
  canUseOfflineCache,
  request,
} from "../src/api/client";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function pendingFetch() {
  const signals: AbortSignal[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          if (init.signal) signals.push(init.signal);
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    ),
  );
  return signals;
}

describe("long administrative requests", () => {
  it("keeps an import or backup alive past the ordinary deadline", async () => {
    vi.useFakeTimers();
    const signals = pendingFetch();
    const outcome = request(
      "http://127.0.0.1:1",
      "token",
      "/v1/leads/import",
      "POST",
      { rows: [] },
      undefined,
      { timeoutMs: LONG_REQUEST_MS },
    ).then(
      () => null,
      (error: unknown) => error,
    );
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1_000);
    expect(signals[0].aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(LONG_REQUEST_MS);
    const error = await outcome;
    expect(signals[0].aborted).toBe(true);
    expect(canUseOfflineCache(error)).toBe(true);
    expect(LONG_REQUEST_MS).toBeGreaterThan(REQUEST_TIMEOUT_MS);
  });
});
