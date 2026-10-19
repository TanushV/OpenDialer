import { afterEach, expect, it, vi } from "vitest";
import { assertNoPendingOptouts } from "../src/api/client";
const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
afterEach(() => {
  vi.unstubAllGlobals();
  invoke.mockReset();
});
it.each([null, "[]"])(
  "allows a verified empty local journal (%s)",
  async (value) => {
    vi.stubGlobal("window", {});
    const getItem = vi.fn().mockReturnValue(value);
    vi.stubGlobal("localStorage", { getItem });
    await assertNoPendingOptouts("tenant", true);
    expect(getItem).toHaveBeenCalledWith("opendialer.optouts.tenant");
  },
);
it.each(['[{"id":"pending"}]', "null", "{}", "broken"])(
  "blocks update or switch with pending or damaged denials (%s)",
  async (value) => {
    vi.stubGlobal("window", {});
    vi.stubGlobal("localStorage", { getItem: () => value });
    await expect(assertNoPendingOptouts("tenant", false)).rejects.toThrow(
      /opt.out/i,
    );
  },
);
it("reads the protected journal in native mode without clearing it", async () => {
  vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
  invoke.mockResolvedValue('[{"id":"pending"}]');
  await expect(assertNoPendingOptouts("tenant", true)).rejects.toThrow(
    /opt.out/i,
  );
  expect(invoke).toHaveBeenCalledTimes(1);
  expect(invoke).toHaveBeenCalledWith("cache_read", {
    namespace: "optouts-tenant",
    simulation: true,
  });
});
