import { defineConfig, devices } from "@playwright/test";

// WebKit is the engine the Mac app runs (R1.2); Chromium stays available with PW_BROWSER=chromium.
const browser = process.env.PW_BROWSER === "chromium" ? "chromium" : "webkit";
// Each worker serves its own frontend and each test its own authority (tests/fixtures.ts).

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
});
