import { spawn } from "node:child_process";
import { once } from "node:events";
import { expect, it, vi } from "vitest";
import { withFixtureServer } from "./fixture-server";

it("stops the simulator if its health check times out", async () => {
  const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"]);
  const exited = once(child, "exit");
  const use = vi.fn();
  try {
    await expect(
      withFixtureServer(child, "http://127.0.0.1:1/health", 0, use),
    ).rejects.toThrow("did not start");
    expect(use).not.toHaveBeenCalled();
    expect(child.killed).toBe(true);
  } finally {
    if (!child.killed) child.kill();
    await exited;
  }
});
