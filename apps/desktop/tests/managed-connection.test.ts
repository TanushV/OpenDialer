import { expect, test } from "vitest";
import { needsSetupFile } from "../src/components/ManagedConnection";

test("the setup file is asked for only while call events are down (R7.4)", () => {
  expect(needsSetupFile(null)).toBe(true);
  expect(
    needsSetupFile({
      configured: true,
      connected: false,
      state: "reconnecting",
    }),
  ).toBe(true);
  expect(
    needsSetupFile({ configured: true, connected: true, state: "connected" }),
  ).toBe(false);
});
