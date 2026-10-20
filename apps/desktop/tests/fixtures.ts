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
export const test = base.extend<{ recoverAfterFailure: void }>({
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
