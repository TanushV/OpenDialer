import { defineConfig, devices } from "@playwright/test";

// WebKit is the engine the Mac app runs (R1.2); Chromium stays available with PW_BROWSER=chromium.
const browser = process.env.PW_BROWSER === "chromium" ? "chromium" : "webkit";
// Each worker serves its own frontend and each test its own authority (tests/fixtures.ts).

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.ts",
  workers: Number(process.env.PW_WORKERS ?? 4),
  fullyParallel: true,
  // One retry, reported as flaky. R5.12 set CI to 0, which exposed older flakes: under load some clicks
  // never reach their handler while the workspace refreshes many times a second. Back to 0 once that is fixed.
  retries: 1,
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
