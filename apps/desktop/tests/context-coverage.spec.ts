import { expect, test } from "./fixtures";

test("compact prospect context discloses omitted history", async ({ page }) => {
  await page.route("**/v1/leads/*/packet", async (route) => {
    const response = await route.fetch();
    const packet = await response.json();
    packet.context_coverage = {
      calls_total: 151,
      calls_omitted: 131,
      commitments_total: 40,
      commitments_omitted: 20,
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
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByRole("button", { name: /^Open lead / })
    .first()
    .click();
  await page
    .getByText("Prior conversation & next step", { exact: true })
    .click();
  await expect(page.getByText(/131 earlier calls/)).toBeVisible();
  await expect(page.getByText(/20 other commitments/)).toBeVisible();
  await expect(page.getByText(/281 other tasks/)).toBeVisible();
});
