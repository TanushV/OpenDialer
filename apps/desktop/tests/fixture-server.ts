import type { ChildProcess } from "node:child_process";

async function ready(url: string, child: ChildProcess, ms: number) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null)
      throw new Error(
        `${url} exited with ${child.exitCode ?? child.signalCode}`,
      );
    try {
      const signal = AbortSignal.timeout(Math.max(1, deadline - Date.now()));
      if ((await fetch(url, { signal })).ok) return;
    } catch {
      /* not listening yet */
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`${url} did not start within ${ms} ms`);
}

async function stop(child: ChildProcess) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise((resolve) => child.once("exit", resolve));
  child.kill("SIGTERM");
  const timer = setTimeout(() => child.kill("SIGKILL"), 5000);
  await exited;
  clearTimeout(timer);
}

export async function withFixtureServer(
  child: ChildProcess,
  healthUrl: string,
  deadlineMs: number,
  use: () => Promise<void>,
) {
  try {
    await ready(healthUrl, child, deadlineMs);
    await use();
  } finally {
    await stop(child);
  }
}
