import { expect, test } from "./fixtures";
import { isolatedSimulationWorkspace } from "./simulation";

test("work time is visible, supported by a heartbeat and correctable after pause", async ({
  page,
}) => {
  await page.clock.install();
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const fresh = await page
    .getByRole("button", { name: "Try a simulated workspace" })
    .isVisible();
  if (fresh)
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  await isolatedSimulationWorkspace(page, "worktime");
  await expect(async () => {
    const check = page.getByRole("button", {
      name: "Check workspace totals",
      exact: true,
    });
    if (await check.isVisible()) await check.click();
    await expect(
      page.getByRole("region", { name: "Calling health" }),
    ).toContainText("Not enough classified calls");
  }).toPass({ timeout: 15000 });
  if (fresh)
    await expect(
      page.getByRole("region", { name: "Calling health" }),
    ).toContainText("Working time not observed");
  const pause = page.getByRole("button", {
    name: "Pause dialing",
    exact: true,
  });
  if (await pause.isVisible()) await pause.click();
  await page.getByRole("button", { name: "Go ready", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Pause dialing", exact: true }),
  ).toBeVisible();
  const beat = page.waitForRequest(
    (r) => r.url().endsWith("/v1/rep/heartbeat") && r.method() === "POST",
  );
  await page.clock.fastForward(61_000);
  expect((await beat).postDataJSON().activity).toBe("ready");
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  // Pausing closes the interval (a new version); review it only after that lands.
  await expect(
    page.getByRole("button", { name: "Go ready", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await page.getByText("Working time & corrections", { exact: true }).click();
  await expect(
    page.getByText(/Heartbeats expire after 90 seconds/),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Review interval", exact: true })
    .first()
    .click();
  const end = await page.getByLabel("Work ended", { exact: true }).inputValue();
  const start = new Date(new Date(end).getTime() - 60_000);
  const localStart = new Date(
    start.getTime() - start.getTimezoneOffset() * 60_000,
  )
    .toISOString()
    .slice(0, 19)
    // Chromium writes whole minutes without ":00"; filling them would be reported as malformed.
    .replace(/:00$/, "");
  await page.getByLabel("Work started", { exact: true }).fill(localStart);
  await page
    .getByLabel("Correction reason", { exact: true })
    .fill("Reviewed actual preparation time");
  await page
    .getByRole("button", { name: "Save working-time correction", exact: true })
    .click();
  await expect(
    page.getByText("Working time corrected", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await page.getByText("Working time & corrections", { exact: true }).click();
  await expect(
    page.getByText("Reviewed actual preparation time", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Calling health" }),
  ).toContainText("1.0 min working time");
});
