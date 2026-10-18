import { expect, test } from "@playwright/test";

test("replay distinguishes technical validation from reviewed usefulness", async ({
  page,
}) => {
  await page.route("**/v1/admin/coaching/replay", (route) =>
    route.fulfill({
      json: {
        samples: 3,
        attempted_samples: 2,
        valid_samples: 1,
        unattempted_samples: 1,
        valid_fraction: 1 / 3,
        expected_action_fraction: 1 / 3,
        p95_inference_ms: 1201,
        semantic_quality: "not_reviewed",
        evidence_scope:
          "synthetic replay; excludes STT, network delivery and hardware audio",
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
  await page
    .getByRole("button", { name: "Refresh learning and capacity" })
    .click();
  await page
    .getByText("Evaluate a reviewed conversation replay", { exact: true })
    .click();
  await page
    .getByLabel("Reviewed replay cases JSON", { exact: true })
    .fill('[{"id":"synthetic"}]');
  await page
    .getByLabel("Replay review reference", { exact: true })
    .fill("Synthetic UI contract");
  await page
    .getByRole("button", { name: "Evaluate reviewed replay", exact: true })
    .click();
  await expect(
    page.getByText(/2 of 3 cases attempted; 1 not attempted/),
  ).toBeVisible();
  await expect(
    page.getByText(/Semantic usefulness has not been reviewed/),
  ).toBeVisible();
  await expect(
    page.getByText(/1201 ms.*includes failed attempts/),
  ).toBeVisible();
  await expect(page.getByText(/Schema and evidence checks/)).toBeVisible();
});
