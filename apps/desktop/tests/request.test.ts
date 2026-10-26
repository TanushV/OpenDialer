import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  canUseOfflineCache,
  localEdit,
  request,
} from "../src/api/client";
import { nextLeadWithRetry } from "../src/state/queue";

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

describe("actionable authority refusals", () => {
  function refuse(status: number, detail: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status,
        json: async () => ({ detail }),
      })),
    );
    return request("http://127.0.0.1:1", null, "/v1/calls/start", "POST").then(
      () => null,
      (error: unknown) => error as ApiError,
    );
  }

  it("explains a missing carrier setup instead of exposing its internal code", async () => {
    const error = await refuse(503, "TELNYX_NOT_CONFIGURED");
    expect(error?.message).toBe(
      "Connect Telnyx in Settings → Setup → Calling before using numbers or calls.",
    );
  });

  it("names invalid setup fields when the authority returns validation errors", async () => {
    const error = await refuse(422, [
      {
        loc: ["body", "maximum_owned_numbers"],
        msg: "Input should be greater than or equal to 0",
      },
    ]);
    expect(error?.message).toContain("maximum owned numbers");
    expect(error?.message).toContain("greater than or equal to 0");
  });

  it("tells the rep to check audio when the authority refuses to start", async () => {
    const error = await refuse(409, {
      code: "REP_AUDIO_NOT_READY",
      message: "Reconnect rep audio, then start the call",
    });
    expect(error?.code).toBe("REP_AUDIO_NOT_READY");
    expect(error?.message).toMatch(/^Check audio before calling/);
  });

  it("points a misconfigured Telnyx credential to its SIP URI setting", async () => {
    const error = await refuse(503, {
      code: "TELEPHONY_CREDENTIAL_MISCONFIGURED",
      message: "Telephony credential rejected the SIP URI call",
    });
    expect(error?.status).toBe(503);
    expect(error?.message).toBe(
      "Telnyx credential needs SIP URI calling set to internal, see Settings.",
    );
  });

  it("keeps an unknown coded refusal's own explanation", async () => {
    const error = await refuse(409, {
      code: "SOMETHING_NEW",
      message: "A specific explanation",
    });
    expect(error?.code).toBe("SOMETHING_NEW");
    expect(error?.message).toBe("A specific explanation");
  });
});

describe("next lead while the queue scan continues", () => {
  const continuing = () =>
    new ApiError("QUEUE_SCAN_CONTINUES", 409, "QUEUE_SCAN_CONTINUES");

  it("retries a bounded number of times and reports the search", async () => {
    const waiting = vi.fn();
    const attempt = vi
      .fn()
      .mockRejectedValueOnce(continuing())
      .mockRejectedValueOnce(continuing())
      .mockResolvedValueOnce({ reservation_id: "found" });
    const result = await nextLeadWithRetry(attempt, {
      delayMs: 0,
      onWaiting: waiting,
    });
    expect(result).toEqual({ reservation_id: "found" });
    expect(attempt).toHaveBeenCalledTimes(3);
    expect(waiting).toHaveBeenCalledWith(true);
    expect(waiting).toHaveBeenLastCalledWith(false);
  });

  it("stops after five tries with a calm explanation", async () => {
    const attempt = vi.fn().mockRejectedValue(continuing());
    await expect(
      nextLeadWithRetry(attempt, { delayMs: 0, onWaiting: () => {} }),
    ).rejects.toThrow(/Still finding an eligible lead/);
    expect(attempt).toHaveBeenCalledTimes(5);
  });

  it("also retries a claim that lost to another rep's concurrent claim", async () => {
    const attempt = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError("QUEUE_CHANGED", 409, "QUEUE_CHANGED"),
      )
      .mockResolvedValueOnce({ reservation_id: "found" });
    await expect(
      nextLeadWithRetry(attempt, { delayMs: 0, onWaiting: () => {} }),
    ).resolves.toEqual({ reservation_id: "found" });
    expect(attempt).toHaveBeenCalledTimes(2);
  });

  it("does not retry any other refusal", async () => {
    const attempt = vi
      .fn()
      .mockRejectedValue(new ApiError("No eligible leads", 409));
    await expect(
      nextLeadWithRetry(attempt, { delayMs: 0, onWaiting: () => {} }),
    ).rejects.toThrow("No eligible leads");
    expect(attempt).toHaveBeenCalledTimes(1);
  });
});
