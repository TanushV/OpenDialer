import { defineConfig, devices } from "@playwright/test";

// WebKit is the engine the Mac app runs (R1.2); Chromium stays available with PW_BROWSER=chromium.
const browser = process.env.PW_BROWSER === "chromium" ? "chromium" : "webkit";
// Each worker serves its own frontend and each test its own authority (tests/fixtures.ts).
const hosted = !!process.env.OPENDIALER_E2E_DATABASE_URL;

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.ts",
  workers: Number(process.env.PW_WORKERS ?? 4),
  fullyParallel: true,
  // Locally, other work on this shared Mac can starve a step; one retry, reported as flaky.
  // CI runs on dedicated runners with no retry, so a real flake still fails there.
  retries: process.env.CI ? 0 : 1,
  // Steps poll the authority; allow for a loaded machine.
  expect: { timeout: 10_000 },
  timeout: 60_000,
  use: {
    ...(browser === "webkit"
      ? devices["Desktop Safari"]
      : devices["Desktop Chrome"]),
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: browser }],
  // Hosted mode needs PostgreSQL; CI-only until Phase 2 removes it.
  webServer: hosted
    ? [
        {
          command:
            "../../.venv/bin/python ../../scripts/e2e_server.py connected",
          url: "http://127.0.0.1:8766/health",
          reuseExistingServer: false,
          // One hosted server for every worker: allow each worker's frontend origin.
          env: {
            OPENDIALER_E2E_ORIGIN: Array.from(
              { length: 16 },
              (_, slot) => `http://127.0.0.1:${11420 + slot}`,
            ).join(","),
          },
        },
      ]
    : [],
});
