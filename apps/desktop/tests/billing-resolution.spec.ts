import { expect, test } from "./fixtures";

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

test("carrier statement import is explicit, repeatable and preserves original record units", async ({
  page,
}) => {
  const posts: unknown[] = [];
  await page.route("**/v1/admin/billing/reconcile", async (route) => {
    posts.push(route.request().postDataJSON());
    if (posts.length === 1)
      await route.fulfill({
        status: 503,
        json: { detail: "Import response lost" },
      });
    else
      await route.fulfill({
        json: {
          matched: 0,
          duplicates: 1,
          duration_seconds: 0,
          evidence: "operator-imported provider records",
          invoice_verified: false,
        },
      });
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
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  const panel = page.getByRole("region", {
    name: "Provider cost reconciliation",
    exact: true,
  });
  await panel
    .getByText("Import carrier statement records", { exact: true })
    .click();
  await panel.getByLabel("Carrier statement file").setInputFiles({
    name: "statement.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "provider_record_id,call_id,leg_id,kind,cost_usd,duration_seconds\nrecord-1,original-call,leg-1,voice,0.02,61",
    ),
  });
  await panel
    .getByLabel("Carrier statement reference")
    .fill("Synthetic statement export");
  expect(posts).toHaveLength(0);
  await panel
    .getByLabel("I checked the call IDs and charges in this statement")
    .check();
  const send = panel.getByRole("button", {
    name: "Import recorded charges",
    exact: true,
  });
  await send.click();
  await expect(panel).toContainText("Import response lost");
  await expect(send).toBeEnabled();
  await send.click();
  await expect(panel).toContainText("1 already recorded");
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  expect(posts[0]).toEqual({
    source_reference: "Synthetic statement export",
    rows: [
      {
        provider_record_id: "record-1",
        call_id: "original-call",
        leg_id: "leg-1",
        kind: "voice",
        cost_usd: 0.02,
        duration_seconds: 61,
      },
    ],
  });
});

test("a confirmed no-charge statement is distinct from a missing carrier receipt", async ({
  page,
}) => {
  let payload: Record<string, unknown> | undefined;
  await page.route("**/v1/admin/billing/ledger?*", (route) =>
    route.fulfill({
      json: {
        entries: [
          {
            id: "empty-call",
            version: 2,
            kind: "call_authorization",
            provider: "telnyx",
            request_id: null,
            request_key: null,
            status: "uncertain",
            reserved_usd: 0.05,
            cost_usd: null,
            credited_usd: 0,
            net_cost_usd: null,
            provider_record_ids: [],
            can_settle: false,
            can_credit: false,
            can_close_call: true,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route(
    "**/v1/admin/billing/ledger/empty-call/reconcile",
    (route) => {
      payload = route.request().postDataJSON();
      return route.fulfill({ json: {} });
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
  await panel.getByLabel("Provider receipt ID").fill("confirmed-zero");
  await panel
    .getByLabel("Statement or source reference")
    .fill("Provider statement explicitly confirms no charge");
  await panel
    .getByLabel("I checked this receipt against this original request")
    .check();
  await panel
    .getByLabel("The imported records cover the complete call statement")
    .check();
  const close = panel.getByRole("button", {
    name: "Confirm complete call costs",
    exact: true,
  });
  await expect(close).toBeDisabled();
  await panel
    .getByLabel(
      "The provider statement explicitly confirms no charge for this call",
    )
    .check();
  await close.click();
  await expect
    .poll(() => payload)
    .toMatchObject({
      action: "close_call",
      cost_usd: 0,
      provider_record_ids: [],
      confirm_complete: true,
      confirm_no_charge: true,
    });
});

test("settled call estimates are shown as estimates, apart from carrier-verified costs", async ({
  page,
}) => {
  const filters: string[] = [];
  const entry = {
    version: 2,
    provider: "telnyx",
    request_id: null,
    credited_usd: 0,
    reserved_usd: 0,
    provider_record_ids: [],
    can_credit: false,
    at: "2026-10-18T10:00:00Z",
  };
  await page.route("**/v1/admin/billing/ledger?*", (route) => {
    const status = new URL(route.request().url()).searchParams.get("status");
    filters.push(status || "");
    route.fulfill({
      json: {
        entries: [
          {
            ...entry,
            id: "estimated-call",
            kind: "call_authorization",
            request_key: "call:estimated",
            status: "estimated",
            cost_usd: 0.0123,
            net_cost_usd: 0.0123,
            billing_verified: false,
            estimate_source: "rate_card",
            can_settle: true,
            can_close_call: true,
          },
          ...(status === "all"
            ? [
                {
                  ...entry,
                  id: "verified-call",
                  kind: "call_authorization",
                  request_key: "call:verified",
                  status: "settled",
                  cost_usd: 0.02,
                  net_cost_usd: 0.02,
                  billing_verified: true,
                  can_settle: false,
                  can_close_call: false,
                },
              ]
            : []),
        ],
        next_cursor: null,
      },
    });
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
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  const panel = page.getByRole("region", {
    name: "Provider cost reconciliation",
    exact: true,
  });
  // The default view lists estimates: they still need the carrier statement.
  const original = panel.getByLabel("Original request");
  await expect(original.locator("option")).toHaveText([
    /call:estimated · Estimated · not carrier-verified/,
  ]);
  await expect(panel).toContainText(
    "Estimated $0.012300 (rate card). Not carrier-verified; record the carrier statement to replace it.",
  );
  await panel.getByLabel("Show costs").selectOption("all");
  await expect(original.locator("option")).toHaveText([
    /call:estimated · Estimated · not carrier-verified/,
    /call:verified · Settled · carrier-verified/,
  ]);
  expect(filters[0]).toBe("unresolved");
});
