import { describe, expect, it } from "vitest";
import { countLabel } from "../src/state/useHistory";

describe("page totals", () => {
  it("shows an exact total as is and a capped total as a lower bound", () => {
    expect(countLabel(42)).toBe("42");
    expect(countLabel(10000, false)).toBe("10000");
    expect(countLabel(10000, true)).toBe(`${(10000).toLocaleString()}+`);
  });
});
