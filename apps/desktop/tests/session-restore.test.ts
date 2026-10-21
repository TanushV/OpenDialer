import { afterEach, expect, it, vi } from "vitest";
import { clearSession, loadSession, saveSession } from "../src/api/client";
const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    removeItem: (key: string) => void values.delete(key),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  invoke.mockReset();
});

it("restores a local workspace from the native store after the WebView profile is reset", async () => {
  vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
  vi.stubGlobal("localStorage", memoryStorage());
  const saved: Record<string, string> = {};
  invoke.mockImplementation(
    async (command: string, args?: { value: string }) => {
      if (command === "save_session") saved.value = args!.value;
      if (command === "load_session") return saved.value ?? null;
      if (command === "clear_session") delete saved.value;
    },
  );
  await saveSession({ connection: "local", simulation: true });
  // A new WebView profile (or a cleared one) has none of the earlier localStorage.
  vi.stubGlobal("localStorage", memoryStorage());
  await expect(loadSession()).resolves.toEqual({
    connection: "local",
    simulation: true,
  });
  await clearSession();
  await expect(loadSession()).resolves.toBeNull();
});

it("still restores an upgraded install whose local choice is only in the WebView", async () => {
  vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
  const storage = memoryStorage();
  storage.setItem(
    "opendialer.mode",
    JSON.stringify({ connection: "local", simulation: false }),
  );
  vi.stubGlobal("localStorage", storage);
  await expect(loadSession()).resolves.toEqual({
    connection: "local",
    simulation: false,
  });
  expect(invoke).not.toHaveBeenCalled();
});
