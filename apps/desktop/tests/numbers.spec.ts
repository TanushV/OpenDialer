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

test("old number order recovery survives reload without another purchase", async ({
  page,
}) => {
  let order = {
    id: "legacy-order",
    version: 1,
    status: "uncertain",
    numbers: ["+14155550198"],
    committed_usd: 1,
    next_action: "Find the provider order ID in your carrier account.",
  };
  const requests: string[] = [];
  await page.route("**/v1/admin/numbers/orders**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      expect(request.url()).toContain("/legacy-order/reconcile");
      expect(request.postDataJSON()).toEqual({
        expected_version: 1,
        provider_order_id: "carrier-order",
      });
      requests.push(request.url());
      order = {
        ...order,
        version: 3,
        status: "completed",
        next_action: "Sync owned inventory and test callbacks.",
      };
      await route.fulfill({ json: order });
    } else await route.fulfill({ json: { orders: [order] } });
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
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  const history = page.getByTestId("number-order-history");
  await expect(
    history.getByText("+14155550198", { exact: true }),
  ).toBeVisible();
  await history.getByLabel("Provider order ID").fill("carrier-order");
  await history
    .getByRole("button", { name: "Reconcile existing order" })
    .click();
  await expect(history.getByText("Completed", { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await expect(history.getByText("Completed", { exact: true })).toBeVisible();
  expect(requests).toHaveLength(1);
  await expect(history.getByText(/unreconciled/i)).toBeVisible();
});

test("order recovery remains retryable after receipt and refresh failures", async ({
  page,
}) => {
  let failed = false;
  await page.route("**/v1/admin/numbers/orders**", async (route) => {
    if (route.request().method() === "POST") failed = true;
    await route.fulfill(
      failed
        ? { status: 503, json: { detail: "Carrier unavailable" } }
        : {
            json: {
              orders: [
                {
                  id: "legacy",
                  version: 1,
                  status: "uncertain",
                  numbers: ["+14155550198"],
                  committed_usd: 1,
                },
              ],
            },
          },
    );
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
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await page.getByRole("button", { name: "Reconcile existing order" }).click();
  await expect(page.getByRole("alert")).toContainText("Carrier unavailable");
  await expect(
    page.getByRole("button", { name: "Reconcile existing order" }),
  ).toBeEnabled();
});

test("declared staffing and setup time survive reload and explain the capacity plan", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (await page.getByRole("button", { name: "Try a simulated workspace" }).isVisible())
    await page.getByRole("button", { name: "Try a simulated workspace" }).click();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByRole("button", { name: "Add staffed period" }).click();
  await page.getByLabel("Staffed start 1").fill("2026-10-19T09:00");
  await page.getByLabel("Staffed end 1").fill("2026-10-19T12:00");
  await page.getByLabel("Available reps 1").fill("2");
  await page.getByLabel("New number setup delay (hours)").fill("24");
  await page.getByRole("button", { name: "Save optimization settings" }).click();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(page.getByLabel("Staffed start 1")).toHaveValue("2026-10-19T09:00");
  await expect(page.getByLabel("Available reps 1")).toHaveValue("2");
  await expect(page.getByLabel("New number setup delay (hours)")).toHaveValue("24");
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await page.getByRole("button", { name: "Plan regional capacity", exact: true }).click();
  const proposal = page.getByTestId("capacity-proposal");
  await expect(proposal.getByText("Declared staffed periods", { exact: true })).toBeVisible();
  await expect(proposal.getByText("Stable-identity shortfall", { exact: true })).toBeVisible();
  await expect(proposal.getByText("Setup-delay shortfall", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByRole("button", { name: "Remove staffed period 1" }).click();
  await page.getByLabel("New number setup delay (hours)").fill("0");
  await page.getByRole("button", { name: "Save optimization settings" }).click();
});
