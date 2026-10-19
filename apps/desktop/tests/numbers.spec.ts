import { expect, test } from "@playwright/test";

for (const connected of [false, true]) {
  test(`${connected ? "connected" : "local"} planning shows pending work then the saved result and explicit simple staffing`, async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    await expect(
      page
        .getByTitle("Sign out")
        .or(page.getByRole("button", { name: "Connect to a server" })),
    ).toBeVisible();
    if (connected) {
      if (await page.getByTitle("Sign out").isVisible())
        await page.getByTitle("Sign out").click();
      await page.getByRole("button", { name: "Connect to a server" }).click();
      await page.getByLabel("Server URL").fill("http://127.0.0.1:8766");
      await page.getByLabel("Email").fill("team@example.test");
      await page
        .getByLabel("Password", { exact: true })
        .fill("Connected-test-password-123");
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
    } else if (
      await page
        .getByRole("button", { name: "Try a simulated workspace" })
        .isVisible()
    ) {
      await page
        .getByRole("button", { name: "Try a simulated workspace" })
        .click();
    }
    await page
      .getByRole("button", { name: "Intelligence", exact: true })
      .click();
    await page.getByLabel("Staffing time zone").fill("America/Los_Angeles");
    await page.getByLabel("Staffing starts (hour)").fill("10");
    await page.getByLabel("Staffing ends (hour)").fill("16");
    await page.getByLabel("Simple staffed reps").fill("2");
    const saved = page.waitForResponse(
      (r) =>
        r.url().endsWith("/v1/admin/optimization") &&
        r.request().method() === "PUT",
    );
    await page
      .getByRole("button", { name: "Save optimization settings" })
      .click();
    expect((await saved).ok()).toBeTruthy();
    await page.reload();
    await page
      .getByRole("button", { name: "Intelligence", exact: true })
      .click();
    await expect(page.getByLabel("Staffing time zone")).toHaveValue(
      "America/Los_Angeles",
    );
    await expect(page.getByLabel("Simple staffed reps")).toHaveValue("2");
    // Reset saved assumptions for subsequent existing tests.
    await page.getByLabel("Staffing time zone").fill("UTC");
    await page.getByLabel("Staffing starts (hour)").fill("9");
    await page.getByLabel("Staffing ends (hour)").fill("20");
    await page.getByLabel("Simple staffed reps").fill("1");
    const reset = page.waitForResponse(
      (r) =>
        r.url().endsWith("/v1/admin/optimization") &&
        r.request().method() === "PUT",
    );
    await page
      .getByRole("button", { name: "Save optimization settings" })
      .click();
    expect((await reset).ok()).toBeTruthy();
    let publish = false;
    await page.route("**/v1/admin/numbers/proposal", async (route) => {
      if (!publish)
        return route.fulfill({
          json: {
            regions: [],
            purchased: false,
            projection: { status: "pending", job_id: "fixture" },
          },
        });
      return route.continue();
    });
    await page.getByRole("button", { name: "Numbers", exact: true }).click();
    await page
      .getByRole("button", { name: "Plan regional capacity", exact: true })
      .click();
    await expect(
      page.getByText("Capacity plan is being prepared in the background.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByTestId("capacity-proposal")).toBeHidden();
    publish = true;
    await page.getByRole("button", { name: "Check capacity plan" }).click();
    await expect(page.getByTestId("capacity-proposal")).toBeVisible();
    await expect(
      page
        .getByTestId("capacity-proposal")
        .getByText("Possible later retries", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByTestId("capacity-proposal").getByText(/Known next attempts/),
    ).toBeVisible();
    await expect(
      page
        .getByTestId("capacity-proposal")
        .getByText("Expansion review", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", {
        name: "Later capacity scenarios",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page
        .getByRole("table", { name: "Conditional retry capacity" })
        .getByRole("row"),
    ).toHaveCount(4);
    await expect(
      page.getByRole("button", { name: "Buy numbers", exact: true }),
    ).toBeHidden();
  });
}

test("incomplete allocation remains readable with unknown costs and bottlenecks", async ({
  page,
}) => {
  const incomplete = {
    forecast_attempts: 100,
    capacity_shortfall: 80,
    existing_only_capacity: 20,
    staffing_shortfall: null,
    number_shortfall: null,
    binding_shortfall: null,
    setup_shortfall: null,
    minimum_additional_numbers: null,
    proposed_additional_numbers: null,
    monthly_rental_estimate_usd: null,
    planning_limited: true,
    optimality: "best_feasible_found",
    optimality_scope: "A bounded incomplete forecast",
    schedule_scope: "declared_staffed_windows",
    regions: [
      {
        region: "general",
        eligible_leads: 100,
        forecast_attempts: 100,
        planned_attempts: 20,
        healthy_existing: 1,
        usable_existing_capacity: 20,
        general_fallback_capacity: 0,
        capacity_waiting_for_review: 0,
        proposed_additional: null,
      },
    ],
  };
  await page.route("**/v1/optimization", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: { ...(await response.json()), forecast: incomplete },
    });
  });
  await page.route("**/v1/admin/numbers/proposal", (route) =>
    route.fulfill({ json: incomplete }),
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
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await expect(
    page.getByText(
      "Allocation is incomplete; remaining bottlenecks are not established.",
    ),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Plan regional capacity", exact: true })
    .click();
  const proposal = page.getByTestId("capacity-proposal");
  await expect(proposal).toBeVisible();
  await expect(
    proposal.getByText("Indicative monthly rental: Not established.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    proposal.getByRole("cell", { name: "Not established", exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "Plan regional capacity", exact: true }),
  ).toBeEnabled();
});

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

test("declared staffing and setup time survive reload and explain the capacity plan", async ({
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByRole("button", { name: "Add staffed period" }).click();
  await page.getByLabel("Staffed start 1").fill("2026-10-19T09:00");
  await page.getByLabel("Staffed end 1").fill("2026-10-19T12:00");
  await page.getByLabel("Available reps 1").fill("2");
  await page.getByLabel("New number setup delay (hours)").fill("24");
  await page
    .getByRole("button", { name: "Save optimization settings" })
    .click();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(page.getByLabel("Staffed start 1")).toHaveValue(
    "2026-10-19T09:00",
  );
  await expect(page.getByLabel("Available reps 1")).toHaveValue("2");
  await expect(page.getByLabel("New number setup delay (hours)")).toHaveValue(
    "24",
  );
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await page
    .getByRole("button", { name: "Plan regional capacity", exact: true })
    .click();
  const proposal = page.getByTestId("capacity-proposal");
  await expect(
    proposal.getByText("Declared staffed periods", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText("Stable-identity shortfall", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText("Setup-delay shortfall", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByRole("button", { name: "Remove staffed period 1" }).click();
  await page.getByLabel("New number setup delay (hours)").fill("0");
  await page
    .getByRole("button", { name: "Save optimization settings" })
    .click();
});

test("named staffing survives reload and explains callbacks without an available owner", async ({
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByRole("button", { name: "Add staffed period" }).click();
  await page.getByLabel("Use named reps 1").check();
  const select = page.getByLabel("Named reps 1", { exact: true });
  const owner = await select.locator("option").first().getAttribute("value");
  expect(owner).toBeTruthy();
  await select.selectOption(owner!);
  await page.getByLabel("Staffed start 1").fill("2026-10-19T09:00");
  await page.getByLabel("Staffed end 1").fill("2026-10-19T12:00");
  const saved = page.waitForRequest(
    (request) =>
      request.method() === "PUT" &&
      request.url().endsWith("/v1/admin/optimization"),
  );
  await page
    .getByRole("button", { name: "Save optimization settings" })
    .click();
  const body = (await saved).postDataJSON();
  expect(body.staffed_windows[0].rep_ids).toEqual([owner]);
  expect(body.staffed_windows[0].reps).toBe(1);
  await expect(
    page.getByRole("button", { name: "Save optimization settings" }),
  ).toBeEnabled();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(page.getByLabel("Use named reps 1")).toBeChecked();
  await expect(page.getByLabel("Named reps 1", { exact: true })).toHaveValues([
    owner!,
  ]);
  await expect(
    page.getByText("Overlapping named periods count each rep once.", {
      exact: false,
    }),
  ).toBeVisible();
  await page.route("**/v1/admin/numbers/proposal", (route) =>
    route.fulfill({
      json: {
        forecast_attempts: 1,
        existing_only_capacity: 0,
        staffing_shortfall: 1,
        number_shortfall: 0,
        binding_shortfall: 0,
        setup_shortfall: 0,
        minimum_additional_numbers: 0,
        proposed_additional_numbers: 0,
        monthly_rental_estimate_usd: 0,
        schedule_scope: "declared_staffed_windows",
        owner_unavailable_attempts: 1,
        unavailable_staff_count: 1,
        occupied_rep_count: 1,
        regions: [],
        optimality_scope:
          "One next attempt per lead on the declared staffing grid",
      },
    }),
  );
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await page
    .getByRole("button", { name: "Plan regional capacity", exact: true })
    .click();
  const proposal = page.getByTestId("capacity-proposal");
  await expect(
    proposal.getByText("Callbacks without owner availability", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText(
      "1 configured rep is unavailable; their time is excluded.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    proposal.getByText(
      "1 rep has an existing call or reservation; occupied time is excluded.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByRole("button", { name: "Remove staffed period 1" }).click();
  await page
    .getByRole("button", { name: "Save optimization settings" })
    .click();
});
