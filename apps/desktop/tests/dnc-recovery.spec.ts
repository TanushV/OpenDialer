import { expect, test } from "@playwright/test";

test("scheduled prepaid screening requires explicit budgets and saves without charged reads", async ({
  page,
}) => {
  let charges = 0;
  await page.route("**/v1/admin/dnc/screen", (route) => {
    charges++;
    return route.fulfill({ json: { results: [] } });
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const enabled = page.getByLabel("Authorize scheduled prepaid DNC checks");
  await expect(enabled).not.toBeChecked();
  await enabled.check();
  await page.getByLabel("DNC maximum per run (USD)").fill("0.05");
  await page.getByLabel("DNC maximum per day (USD)").fill("0.10");
  await page
    .getByRole("button", { name: "Save privacy and usage policy" })
    .click();
  await expect(
    page.getByText("Privacy and usage policy saved", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(enabled).toBeChecked();
  await expect(page.getByLabel("DNC maximum per run (USD)")).toHaveValue(
    "0.05",
  );
  expect(charges).toBe(0);
  await enabled.uncheck();
  await page
    .getByRole("button", { name: "Save privacy and usage policy" })
    .click();
  await expect(
    page.getByText("Privacy and usage policy saved", { exact: true }),
  ).toBeVisible();
});

test("prepaid screening recovery needs a charge choice and preserves retry identity", async ({
  page,
}) => {
  let completed = false;
  const posts: unknown[] = [];
  await page.route("**/v1/admin/dnc/screens", (route) =>
    route.fulfill({
      json: {
        screens: [
          {
            id: "uncertain-screen",
            version: 4,
            phone: "+14155550101",
            status: completed ? "completed" : "uncertain",
            listed: completed ? false : null,
            can_retry: !completed,
            possible_charge_usd: 0.001667,
            reason: "Lookup result was lost; a previous charge may remain.",
          },
        ],
        limited: false,
      },
    }),
  );
  await page.route(
    "**/v1/admin/dnc/screens/uncertain-screen/retry",
    async (route) => {
      const body = route.request().postDataJSON();
      posts.push(body);
      expect(body.expected_version).toBe(4);
      expect(body.accept_possible_duplicate_charge).toBe(true);
      expect(body.idempotency_key).toBeTruthy();
      if (posts.length === 1)
        await route.fulfill({
          status: 503,
          json: { detail: "Screening unavailable" },
        });
      else {
        completed = true;
        await route.fulfill({ json: { status: "completed" } });
      }
    },
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const panel = page.getByRole("region", {
    name: "Prepaid screening results",
    exact: true,
  });
  await expect(panel).toContainText("previous charge may remain");
  expect(posts).toHaveLength(0);
  const retry = panel.getByRole("button", {
    name: "Retry prepaid check",
    exact: true,
  });
  await expect(retry).toBeDisabled();
  await panel
    .getByLabel("I accept that another query may charge another credit")
    .check();
  await retry.click();
  await expect(panel).toContainText("Screening unavailable");
  await expect(retry).toBeEnabled();
  await retry.click();
  await expect(panel).toContainText("Not listed in this national check");
  await expect(retry).toHaveCount(0);
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(panel).toContainText("Not listed in this national check");
  expect(posts).toHaveLength(2);
});
