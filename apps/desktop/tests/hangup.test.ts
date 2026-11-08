import { expect, test } from "vitest";
import { endedSummary } from "../src/state/hangup";

test("an ended call says who hung up and how long it lasted", () => {
  // R0.10: the dock said "Ready when you are" after a prospect hung up.
  expect(
    endedSummary({
      state: "ENDED",
      provider_hangup_source: "callee",
      connected_seconds: 23.4,
    }),
  ).toBe("They hung up · 0:23");
  expect(
    endedSummary({
      state: "ENDED",
      provider_hangup_source: "caller",
      connected_seconds: 61,
    }),
  ).toBe("You ended the call · 1:01");
  expect(endedSummary({ state: "ENDED" })).toBe("Call ended");
  expect(endedSummary({ state: "CONNECTED" })).toBeNull();
  expect(endedSummary(null)).toBeNull();
});
