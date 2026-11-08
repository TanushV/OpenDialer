import { expect, test } from "vitest";
import { date } from "../src/context";

test("dates read the same in every engine and name their zone", () => {
  // WebKit wrote "Oct 21 at 2:00 PM EDT" where Chromium wrote "Oct 21, 2:00 PM EDT".
  expect(date("2026-10-21T18:00:00+00:00", "America/New_York")).toBe(
    "Oct 21, 2:00 PM EDT",
  );
  expect(date("2026-10-21T07:05:00+00:00", "America/Los_Angeles")).toBe(
    "Oct 21, 12:05 AM PDT",
  );
  expect(date(null)).toBe("Needs confirmation");
});
