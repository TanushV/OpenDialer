import { describe, expect, it } from "vitest";
import {
  chooseZone,
  localTime,
  zonedInput,
  zonedInstant,
} from "../src/state/zones";

describe("prospect time zones", () => {
  it("enters a callback in the lead's zone regardless of the device zone", () => {
    expect(zonedInstant("2026-11-03T14:00", "America/Los_Angeles")).toBe(
      "2026-11-03T22:00:00.000Z",
    );
    expect(zonedInstant("2026-11-03T14:00", "America/New_York")).toBe(
      "2026-11-03T19:00:00.000Z",
    );
    expect(zonedInput("2026-11-03T22:00:00Z", "America/Los_Angeles")).toBe(
      "2026-11-03T14:00",
    );
  });
  it("round-trips across a daylight-saving change and rejects a skipped hour", () => {
    expect(zonedInstant("2026-11-01T01:30", "America/Chicago")).toBe(
      "2026-11-01T06:30:00.000Z",
    );
    expect(
      zonedInput(
        zonedInstant("2026-03-09T09:15", "America/Denver"),
        "America/Denver",
      ),
    ).toBe("2026-03-09T09:15");
    expect(() => zonedInstant("2026-03-08T02:30", "America/New_York")).toThrow(
      /daylight-saving/,
    );
    expect(() => zonedInstant("2026-03-08", "America/New_York")).toThrow();
  });
  it("prefers an explicit zone and labels a device fallback", () => {
    expect(chooseZone(null, "Bad/Zone", "Europe/Paris")).toEqual({
      zone: "Europe/Paris",
      explicit: true,
    });
    expect(chooseZone(undefined, null).explicit).toBe(false);
    expect(localTime(null)).toBeNull();
    expect(localTime("Asia/Tokyo", Date.parse("2026-10-14T18:00:00Z"))).toMatch(
      /3:00/,
    );
  });
});
