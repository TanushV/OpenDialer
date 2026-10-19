import { describe, expect, it, vi } from "vitest";
import { ApiError, localEdit } from "../src/api/client";

describe("local edit receipts", () => {
  it("replays one lost response with an immutable original identity and version", async () => {
    const values = {
      request_id: "ebae11e5-fdc6-43dc-8df1-0fd513204df3",
      expected_version: 2,
      note: "Human decision",
    };
    const api = vi
      .fn()
      .mockImplementationOnce(async () => {
        values.note = "Changed while waiting";
        throw new TypeError("Synthetic lost response");
      })
      .mockResolvedValueOnce({
        id: "task",
        version: 3,
        edit_receipt: { request_id: values.request_id },
      });
    const saved = await localEdit(api, "/v1/tasks/task", "PATCH", values);
    expect(saved).toHaveProperty("version", 3);
    expect(api).toHaveBeenCalledTimes(2);
    expect(api.mock.calls[1]).toEqual(api.mock.calls[0]);
    expect(api.mock.calls[1][2].note).toBe("Human decision");
  });

  it.each([403, 404, 409, 422])(
    "does not retry a rejected edit (%i)",
    async (status) => {
      const api = vi.fn().mockRejectedValue(new ApiError("Rejected", status));
      await expect(
        localEdit(api, "/v1/tasks/task", "PATCH", {
          request_id: crypto.randomUUID(),
          expected_version: 2,
          status: "completed",
        }),
      ).rejects.toThrow("Rejected");
      expect(api).toHaveBeenCalledTimes(1);
    },
  );

  it("bounds retries and never applies this policy to carrier or paid operations", async () => {
    const api = vi.fn().mockRejectedValue(new TypeError("Offline"));
    const values = {
      request_id: crypto.randomUUID(),
      expected_version: 2,
      status: "completed",
    };
    await expect(
      localEdit(api, "/v1/tasks/task", "PATCH", values),
    ).rejects.toThrow("Offline");
    expect(api).toHaveBeenCalledTimes(2);
    api.mockClear();
    await expect(
      localEdit(api, "/v1/calls/start", "POST", values),
    ).rejects.toThrow(/local edit/i);
    expect(api).not.toHaveBeenCalled();
  });
});
