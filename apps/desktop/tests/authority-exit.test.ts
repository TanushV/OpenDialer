import { afterEach, expect, it, vi } from "vitest";
import { assertNoPendingOptouts } from "../src/api/client";
afterEach(() => vi.unstubAllGlobals());
it.each([null, "[]"])("allows an empty local opt-out queue (%s)", (value) => {
  const getItem = vi.fn().mockReturnValue(value);
  vi.stubGlobal("localStorage", { getItem });
  assertNoPendingOptouts("tenant");
  expect(getItem).toHaveBeenCalledWith("opendialer.optouts.tenant");
});
it.each(['[{"id":"pending"}]', "null", "{}", "broken"])(
  "blocks update or switch with pending or damaged denials (%s)",
  (value) => {
    vi.stubGlobal("localStorage", { getItem: () => value });
    expect(() => assertNoPendingOptouts("tenant")).toThrow(/opt.out/i);
  },
);
