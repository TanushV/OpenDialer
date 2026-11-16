import { expect, test as base } from "@playwright/test";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { withFixtureServer } from "./fixture-server";

export { expect };
export type { Page } from "@playwright/test";

/**
 * Each worker serves its own frontend, and each test gets a fresh local authority with its own
 * SQLite file (R1.2a). Tests never share state, so they run in parallel and need no cleanup.
 */
const DESKTOP = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const ROOT = path.resolve(DESKTOP, "../..");
const slot = Number(process.env.TEST_PARALLEL_INDEX ?? 0);
const BACKEND_PORT = 18765 + slot;
const FRONTEND_PORT = 11420 + slot;
/** This worker's local authority. */
export const AUTHORITY = `http://127.0.0.1:${BACKEND_PORT}`;

/**
 * Set up this test's authority the way an earlier session would have, so the app opens on its
 * restore path instead of the first-run choice.
 */
export async function setUpAuthority(page: import("@playwright/test").Page) {
  const headers = { "X-Local-Token": "e2e-local-token" };
  const setup = await page.request.post(`${AUTHORITY}/v1/setup`, {
    headers,
    data: {
      seller_name: "Example Seller",
      campaign_name: "A better next conversation",
      name: "Alex Morgan",
    },
  });
  expect(setup.ok()).toBeTruthy();
  const { access_token } = await setup.json();
  const seeded = await page.request.post(`${AUTHORITY}/v1/simulation/seed`, {
    headers: { authorization: `Bearer ${access_token}` },
  });
  expect(seeded.ok()).toBeTruthy();
}

// The UI aborts superseded loads (and React StrictMode remounts every loader in
// development), so a fixture route can lose its request while it is still
// fetching. A cancelled request is not a test failure; any other error is.
const cancelled = /Route is already handled/;

export const test = base.extend<
  { authority: string; releaseRoutes: void },
  { frontend: string }
>({
  frontend: [
    async ({}, use) => {
      const vite = spawn(
        process.execPath,
        [
          path.join(DESKTOP, "node_modules/vite/bin/vite.js"),
          "--host",
          "127.0.0.1",
          "--port",
          String(FRONTEND_PORT),
          "--strictPort",
        ],
        {
          cwd: DESKTOP,
          env: {
            ...process.env,
            VITE_BACKEND_URL: AUTHORITY,
            VITE_LOCAL_TOKEN: "e2e-local-token",
          },
          stdio: "ignore",
        },
      );
      const url = `http://127.0.0.1:${FRONTEND_PORT}`;
      await withFixtureServer(vite, url, 60_000, () => use(url));
    },
    { scope: "worker", timeout: 90_000 },
  ],
  authority: [
    async ({}, use) => {
      const server = spawn(
        path.join(ROOT, ".venv/bin/python"),
        [path.join(ROOT, "scripts/e2e_server.py"), "local"],
        {
          env: {
            ...process.env,
            OPENDIALER_E2E_PORT: String(BACKEND_PORT),
            OPENDIALER_E2E_ORIGIN: `http://127.0.0.1:${FRONTEND_PORT}`,
          },
          stdio: "ignore",
        },
      );
      await withFixtureServer(server, `${AUTHORITY}/health`, 30_000, () =>
        use(AUTHORITY),
      );
    },
    { auto: true, timeout: 45_000 },
  ],
  baseURL: async ({ frontend }, use) => use(frontend),
  page: async ({ page, authority }, use) => {
    void authority;
    const route = page.route.bind(page);
    page.route = ((url, handler, options) =>
      route(
        url,
        async (fixture, request) => {
          try {
            await handler(fixture, request);
          } catch (error) {
            if (!cancelled.test(String(error))) throw error;
          }
        },
        options,
      )) as typeof page.route;
    await use(page);
  },
  releaseRoutes: [
    async ({ page }, use) => {
      await use();
      // A fixture route still handling a background read when the page closes
      // would otherwise fail a finished test with "Response has been disposed".
      await page.unrouteAll({ behavior: "ignoreErrors" });
    },
    { auto: true },
  ],
});
