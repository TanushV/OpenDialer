import { describe, expect, it } from "vitest";
import { ApiError, canUseOfflineCache } from "../src/api/client";
describe("protected offline cache", () => {
  it("cannot turn an authenticated rejection into an offline cache hit", () => {
    expect(canUseOfflineCache(new ApiError("revoked", 401))).toBe(false);
    expect(canUseOfflineCache(new ApiError("forbidden", 403))).toBe(false);
    expect(canUseOfflineCache(new TypeError("Network failed"))).toBe(true);
    expect(canUseOfflineCache(new Error("Invalid content"))).toBe(false);
  });
});
