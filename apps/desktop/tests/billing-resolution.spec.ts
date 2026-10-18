import { expect, test } from "@playwright/test";

test("an imported receipt settles the original liability with recoverable lost-response retry", async ({
  page,
}) => {
  const posts: Record<string, unknown>[] = [];
  let settled = false;
  await page.route("**/v1/admin/billing/ledger?*", (route) =>
    route.fulfill({
      json: {
        entries: [
          {
            id: "uncertain-original",
            version: 3,
            kind: "live_coach",
            work_kind: "live_coach",
            provider: "openrouter",
            request_id: "provider-original",
            request_key: "local-original",
            status: settled ? "settled" : "uncertain",
            reserved_usd: settled ? 0 : 0.02,
            cost_usd: settled ? 0.003 : null,
            credited_usd: 0,
            net_cost_usd: settled ? 0.003 : null,
            at: "2026-10-18T10:00:00Z",
            can_settle: !settled,
            can_credit: settled,
            can_close_call: false,
            provider_record_ids: [],
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route(
    "**/v1/admin/billing/ledger/uncertain-original/reconcile",
    async (route) => {
      posts.push(route.request().postDataJSON());
      if (posts.length === 1)
        await route.fulfill({
          status: 503,
          json: { detail: "Receipt response lost; retry the same receipt" },
        });
      else {
        settled = true;
        await route.fulfill({
          json: {
            entry: { id: "uncertain-original" },
            evidence: "operator-imported provider record",
            invoice_verified: false,
          },
        });
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
  const panel = page.getByRole("region", {
    name: "Provider cost reconciliation",
    exact: true,
  });
  await expect(panel).toContainText("local-original");
  expect(posts).toHaveLength(0);
  await panel.getByLabel("Provider receipt ID").fill("statement-line-one");
  await panel
    .getByLabel("Statement or source reference")
    .fill("Synthetic invoice line 1");
  await panel.getByLabel("Amount (USD)").fill("0.003");
  const save = panel.getByRole("button", {
    name: "Record receipt",
    exact: true,
  });
  await expect(save).toBeDisabled();
  await panel
    .getByLabel("I checked this receipt against this original request")
    .check();
  await save.click();
  await expect(panel).toContainText("Receipt response lost");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(panel).toContainText("0.003000");
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  expect(posts[0]).toMatchObject({
    action: "settle",
    expected_version: 3,
    provider: "openrouter",
    request_id: "provider-original",
    receipt_id: "statement-line-one",
    cost_usd: 0.003,
    attest_original: true,
  });
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await expect(panel).toContainText("0.003000");
  expect(posts).toHaveLength(2);
});
