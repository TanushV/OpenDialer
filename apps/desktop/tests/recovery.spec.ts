import { expect, test } from "@playwright/test";

test("recovery offers verified reconciliation and keeps failed actions retryable", async ({
  page,
}) => {
  let reconciled = false;
  let submissions = 0;
  await page.route("**/v1/admin/recovery", async (route) => {
    await route.fulfill({
      json: {
        commands: reconciled
          ? []
          : [
              {
                id: "uncertain-command",
                version: 3,
                kind: "dial",
                status: "review_required",
                call_id: "uncertain-call",
                provider_id: null,
                reason: "No matching carrier status receipt",
                can_end: false,
                can_cancel: false,
              },
            ],
        agents: [],
        jobs: [],
        limited: false,
      },
    });
  });
  await page.route(
    "**/v1/admin/recovery/commands/uncertain-command",
    async (route) => {
      submissions++;
      expect(route.request().postDataJSON()).toEqual({
        expected_version: 3,
        action: "reconcile",
        provider_id: "original-carrier-reference",
      });
      if (submissions === 1) {
        await route.fulfill({
          status: 503,
          json: { detail: "Carrier status unavailable" },
        });
      } else {
        reconciled = true;
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
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  const recovery = page.getByRole("region", {
    name: "Work needing recovery",
    exact: true,
  });
  await expect(recovery).toContainText("No matching carrier status receipt");
  await recovery
    .getByLabel("Original carrier reference")
    .fill("original-carrier-reference");
  await recovery
    .getByRole("button", { name: "Verify carrier status", exact: true })
    .click();
  await expect(recovery).toContainText("Carrier status unavailable");
  await expect(
    recovery.getByRole("button", {
      name: "Verify carrier status",
      exact: true,
    }),
  ).toBeEnabled();
  await recovery
    .getByRole("button", { name: "Verify carrier status", exact: true })
    .click();
  await expect(recovery).toContainText("No work needs recovery");
  expect(submissions).toBe(2);
});
