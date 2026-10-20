import { expect, test } from "./fixtures";
import { leaveNoCall, prepareSimulationCall } from "./simulation";

test.afterEach(async ({ page }) => leaveNoCall(page));

async function enter(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  )
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
}

test("next lead keeps looking while the authority's queue scan continues", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Queue scan fixture", "+14155550887");
  let requests = 0;
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/queue/next", async (route) => {
    requests++;
    if (requests <= 2) {
      if (requests === 2) await held;
      await route.fulfill({
        status: 409,
        json: {
          detail: {
            code: "QUEUE_SCAN_CONTINUES",
            reasons: ["registry_coverage"],
            retryable: true,
          },
        },
      });
      return;
    }
    await route.continue();
  });
  try {
    await page
      .getByRole("button", { name: "Get next lead", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Finding an eligible lead…" }),
    ).toBeDisabled();
    release();
    await expect(
      page.getByRole("button", { name: "Start call", exact: true }),
    ).toBeVisible();
    expect(requests).toBe(3);
    await expect(page.getByText(/QUEUE_SCAN_CONTINUES/)).toHaveCount(0);
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("a start refused for rep audio tells the rep to check audio", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Audio refusal fixture", "+14155550888");
  await page.route("**/v1/calls/start", (route) =>
    route.fulfill({
      status: 409,
      json: {
        detail: {
          code: "REP_AUDIO_NOT_READY",
          message: "Reconnect rep audio, then start the call",
        },
      },
    }),
  );
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(
    page.getByText(/^Check audio before calling\./).first(),
  ).toBeVisible();
  await expect(page.getByText(/REP_AUDIO_NOT_READY/)).toHaveCount(0);
});
