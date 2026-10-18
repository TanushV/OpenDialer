import { expect, test } from "@playwright/test";

test("number capacity distinguishes bottlenecks and quotes cannot start rentals", async ({
  page,
}) => {
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
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await page
    .getByRole("button", { name: "Plan regional capacity", exact: true })
    .click();
  const proposal = page.getByTestId("capacity-proposal");
  await expect(
    proposal.getByText("Existing-only capacity", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText("Staffing shortfall", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText("Number shortfall", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText("Minimum additional numbers", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Procurement area code").fill("415");
  await page.getByRole("button", { name: "Get carrier quotes" }).click();
  await expect(page.getByText("+14155550188", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /purchase/i })).toHaveCount(0);
  await expect(
    page.getByText("New rentals blocked", { exact: true }),
  ).toBeVisible();
});
