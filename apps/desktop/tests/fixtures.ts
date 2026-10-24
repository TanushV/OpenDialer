import { expect, test as base } from "@playwright/test";
import { leaveNoCall } from "./simulation";

export { expect };
export type { Page } from "@playwright/test";

/**
 * Every spec shares the two e2e authorities. A journey that fails mid-call
 * must not leave its call, wrap-up or reservation for the next test, so a
 * failed test reopens the workspace and ends what it left behind. Passing
 * journeys already clean up after themselves, so they pay no navigation here.
 */
// The UI aborts superseded loads (and React StrictMode remounts every loader in
// development), so a fixture route can lose its request while it is still
// fetching. A cancelled request is not a test failure; any other error is.
const cancelled = /Route is already handled/;

export const test = base.extend<{ recoverAfterFailure: void }>({
  page: async ({ page }, use) => {
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
  recoverAfterFailure: [
    async ({ page }, use, testInfo) => {
      await use();
      // A fixture route still handling a background read when the page closes
      // would otherwise fail a finished test with "Response has been disposed".
      await page.unrouteAll({ behavior: "ignoreErrors" });
      if (testInfo.status !== testInfo.expectedStatus)
        await leaveNoCall(page).catch(() => {
          /* The failure itself is already reported. */
        });
    },
    { auto: true, timeout: 30_000 },
  ],
});
