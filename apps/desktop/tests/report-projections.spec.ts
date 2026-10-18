import { expect, test } from "@playwright/test";

test("an unfinished report shows pending evidence without invented totals", async ({
  page,
}) => {
  await page.route("**/v1/optimization", (route) =>
    route.fulfill({
      json: {
        settings: {},
        numbers: [],
        projection: {
          status: "pending",
          as_of: null,
          refresh_status: "queued",
          job_id: "report-job",
        },
      },
    }),
  );
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(
    page.getByText("Preparing the first report in the background.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText(/0 unique conversions/)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Check report status", exact: true }),
  ).toBeVisible();
});
