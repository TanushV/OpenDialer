import { expect, type Page } from "@playwright/test";

/** Give a calling journey its own fictional work without resetting shared state. */
export async function prepareSimulationCall(
  page: Page,
  name: string,
  phone: string,
) {
  await expect(
    page.getByText(/Practice workspace · Calls and provider responses/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("CSV lead data")
    .fill(
      `name,phone,company,timezone,region\n${name},${phone},Example,America/Los_Angeles,415`,
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/1 imported/)).toBeVisible();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  const ready = page.getByRole("button", { name: "Go ready", exact: true });
  await expect(
    ready.or(page.getByRole("button", { name: "Pause dialing", exact: true })),
  ).toBeVisible();
  if (await ready.isVisible()) await ready.click();
}
