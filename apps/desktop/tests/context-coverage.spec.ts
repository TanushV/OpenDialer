import { expect, test } from "./fixtures";

test("compact prospect context discloses omitted history", async ({ page }) => {
  await page.route("**/v1/leads/*/packet", async (route) => {
    const response = await route.fetch();
    const packet = await response.json();
    packet.context_coverage = {
      calls_total: 151,
      calls_omitted: 131,
      tasks_total: 301,
      tasks_omitted: 281,
    };
    await route.fulfill({ response, json: packet });
  });
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByRole("button", { name: /^Open lead / })
    .first()
    .click();
  await page.getByText("Earlier calls", { exact: true }).click();
  await expect(page.getByText(/131 older calls are not shown/)).toBeVisible();
});
