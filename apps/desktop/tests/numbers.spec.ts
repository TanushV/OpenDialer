import { expect, test } from "./fixtures";
import { openMoreSettings } from "./navigation";

test("number review distinguishes operational evidence from unknown external reputation", async ({
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
  for (const state of [
    "Ready",
    "Pending setup",
    "Limited use",
    "Paused outbound",
  ])
    await expect(
      page
        .getByRole("region", { name: "Inventory status" })
        .getByText(state, { exact: true }),
    ).toBeVisible();
  await page
    .getByRole("button", { name: "Review number evidence" })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "Number evidence and workload" }),
  ).toBeVisible();
  await expect(
    page.getByText("External label unknown", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Comparable classified attempts", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Reviewed daily limit")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Advance stage", exact: true }),
  ).toBeHidden();
  await expect(
    page.getByText("A note alone cannot verify a callback.", { exact: true }),
  ).toBeVisible();
});

for (const connected of [false, true]) {
  test(`${connected ? "connected" : "local"} number review recovers a stale edit and saves evidence policy`, async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
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
    await page.getByRole("button", { name: "Numbers", exact: true }).click();
    await page
      .getByRole("button", { name: "Review number evidence" })
      .first()
      .click();
    const original = Number(
      await page.getByLabel("Reviewed daily limit").inputValue(),
    );
    await page.getByLabel("Number review decision").selectOption("reduce");
    await page.getByLabel("Reviewed daily limit").fill(String(original - 1));
    await page
      .getByLabel("Number review reference")
      .fill("Operator reviewed available workload");
    let blocked = true;
    await page.route("**/v1/admin/numbers/*/review", async (route) => {
      if (blocked && route.request().method() === "POST") {
        blocked = false;
        return route.fulfill({
          status: 409,
          json: { detail: "Number changed; reload evidence" },
        });
      }
      return route.continue();
    });
    await page.getByRole("button", { name: "Apply number review" }).click();
    await expect(page.getByRole("alert")).toContainText(
      "Number changed; reload evidence",
    );
    await expect(
      page.getByRole("button", { name: "Apply number review" }),
    ).toBeEnabled();
    await page.getByRole("button", { name: "Reload evidence" }).click();
    await expect(page.getByLabel("Reviewed daily limit")).toHaveValue(
      String(original),
    );
    await page.getByLabel("Reviewed daily limit").fill(String(original - 1));
    const saved = page.waitForResponse(
      (r) => r.url().endsWith("/review") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Apply number review" }).click();
    expect((await saved).ok()).toBeTruthy();
    await page.reload();
    await page.getByRole("button", { name: "Numbers", exact: true }).click();
    await page
      .getByRole("button", { name: "Review number evidence" })
      .first()
      .click();
    await expect(page.getByLabel("Reviewed daily limit")).toHaveValue(
      String(original - 1),
    );
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByText("Advanced", { exact: true }).click();
    await page
      .getByText("Advanced number evidence policy", { exact: true })
      .click();
    await page.getByLabel("Callback proof lifetime (days)").fill("17");
    const policy = page.waitForResponse(
      (r) =>
        r.url().endsWith("/v1/admin/optimization") &&
        r.request().method() === "PUT",
    );
    await page
      .getByRole("button", { name: "Save planning assumptions" })
      .click();
    expect((await policy).ok()).toBeTruthy();
    await page.reload();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByText("Advanced", { exact: true }).click();
    await page
      .getByText("Advanced number evidence policy", { exact: true })
      .click();
    await expect(page.getByLabel("Callback proof lifetime (days)")).toHaveValue(
      "17",
    );
  });
}

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
    const schedule = async () => {
      await openMoreSettings(page, "Campaign goal and team schedule");
    };
    await schedule();
    await page
      .getByRole("combobox", { name: "Staffing time zone" })
      .selectOption("America/Los_Angeles");
    await page.getByLabel("Staffing starts (hour)").fill("10");
    await page.getByLabel("Staffing ends (hour)").fill("16");
    await page.getByLabel("Simple staffed reps").fill("2");
    const saved = page.waitForResponse(
      (r) =>
        r.url().endsWith("/v1/admin/optimization") &&
        r.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "Save rep schedule" }).click();
    expect((await saved).ok()).toBeTruthy();
    await page.reload();
    await schedule();
    await expect(page.getByLabel("Staffing time zone")).toHaveValue(
      "America/Los_Angeles",
    );
    await expect(page.getByLabel("Simple staffed reps")).toHaveValue("2");
    // Reset saved assumptions for subsequent existing tests.
    await page
      .getByRole("combobox", { name: "Staffing time zone" })
      .selectOption("UTC");
    await page.getByLabel("Staffing starts (hour)").fill("9");
    await page.getByLabel("Staffing ends (hour)").fill("20");
    await page.getByLabel("Simple staffed reps").fill("1");
    const reset = page.waitForResponse(
      (r) =>
        r.url().endsWith("/v1/admin/optimization") &&
        r.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "Save rep schedule" }).click();
    expect((await reset).ok()).toBeTruthy();
    // The saved plan is read with GET; a queued job is shown as pending.
    let publish = false;
    await page.route("**/v1/admin/numbers/summary", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      if (!publish)
        body.forecast = {
          regions: [],
          staffing_shortfall: null,
          number_shortfall: null,
          projection: {
            status: "pending",
            job_id: "fixture",
            refresh_status: "queued",
          },
        };
      await route.fulfill({ response, json: body });
    });
    await page.getByRole("button", { name: "Numbers", exact: true }).click();
    await page
      .getByRole("button", { name: "Plan state coverage", exact: true })
      .click();
    await expect(
      page.getByText("Capacity plan is being prepared in the background.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByTestId("capacity-proposal")).toBeHidden();
    publish = true;
    await page.getByRole("button", { name: "Check capacity plan" }).click();
    await expect(page.getByTestId("capacity-proposal")).toBeVisible({
      timeout: 20000,
    });
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
  await page.route("**/v1/admin/numbers/summary", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: {
        ...(await response.json()),
        forecast: {
          ...incomplete,
          projection: { status: "ready", as_of: "2026-10-14T18:00:00Z" },
        },
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
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await expect(
    page.getByText(
      /Allocation is incomplete; remaining bottlenecks are not established./,
    ),
  ).toBeVisible();
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
    page.getByRole("button", { name: "Plan state coverage", exact: true }),
  ).toBeEnabled();
});

test("number capacity distinguishes bottlenecks without searching or ordering rentals", async ({
  page,
}) => {
  const rentalRequests: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/numbers\/(search|purchase)$/.test(r.url()))
      rentalRequests.push(r.url());
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
  await page
    .getByRole("button", { name: "Plan state coverage", exact: true })
    .click();
  const proposal = page.getByTestId("capacity-proposal");
  await expect(
    proposal.getByText("Existing-only capacity", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await expect(
    proposal.getByText("Staffing shortfall", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText("Number shortfall", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText("Minimum additional numbers", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Procurement area code")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /purchase/i })).toHaveCount(0);
  await expect(
    proposal.getByText(/This plan does not purchase numbers/),
  ).toBeVisible();
  expect(rentalRequests).toEqual([]);
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
  await openAdvanced(page);
  await page.getByRole("button", { name: "Add staffed period" }).click();
  await page.getByLabel("Staffed start 1").fill("2026-10-19T09:00");
  await page.getByLabel("Staffed end 1").fill("2026-10-19T12:00");
  await page.getByLabel("Available reps 1").fill("2");
  await page.getByLabel("New number setup delay (hours)").fill("24");
  await page.getByRole("button", { name: "Save planning assumptions" }).click();
  await expect(
    page.getByText("Planning assumptions saved").first(),
  ).toBeVisible();
  await page.reload();
  await openAdvanced(page);
  await expect(page.getByLabel("Staffed start 1")).toHaveValue(
    "2026-10-19T09:00",
  );
  await expect(page.getByLabel("Available reps 1")).toHaveValue("2");
  await expect(page.getByLabel("New number setup delay (hours)")).toHaveValue(
    "24",
  );
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await page
    .getByRole("button", { name: "Plan state coverage", exact: true })
    .click();
  const proposal = page.getByTestId("capacity-proposal");
  await expect(
    proposal.getByText("Declared staffed periods", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await expect(
    proposal.getByText("Stable-identity shortfall", { exact: true }),
  ).toBeVisible();
  await expect(
    proposal.getByText("Setup-delay shortfall", { exact: true }),
  ).toBeVisible();
  await openAdvanced(page);
  await page.getByRole("button", { name: "Remove staffed period 1" }).click();
  await page.getByLabel("New number setup delay (hours)").fill("0");
  await clearStaffing(page);
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
  await openAdvanced(page);
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
  await page.getByRole("button", { name: "Save planning assumptions" }).click();
  const body = (await saved).postDataJSON();
  expect(body.staffed_windows).toHaveLength(1);
  expect(body.staffed_windows[0].start_at).toBe("2026-10-19T09:00:00.000Z");
  expect(body.staffed_windows[0].end_at).toBe("2026-10-19T12:00:00.000Z");
  expect(body.staffed_windows[0].rep_ids).toEqual([owner]);
  expect(body.staffed_windows[0].reps).toBe(1);
  await expect(
    page.getByRole("button", { name: "Save planning assumptions" }),
  ).toBeEnabled();
  await expect(
    page.getByText("Planning assumptions saved").first(),
  ).toBeVisible();
  await page.reload();
  await openAdvanced(page);
  await expect(page.getByLabel("Use named reps 1")).toBeChecked();
  await expect(page.getByLabel("Named reps 1", { exact: true })).toHaveValues([
    owner!,
  ]);
  await expect(
    page.getByText("Overlapping named periods count each rep once.", {
      exact: false,
    }),
  ).toBeVisible();
  await page.route("**/v1/admin/numbers/summary", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: {
        ...(await response.json()),
        forecast: {
          projection: { status: "ready", as_of: "2026-10-14T18:00:00Z" },
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
      },
    });
  });
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
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
  await openAdvanced(page);
  await page.getByRole("button", { name: "Remove staffed period 1" }).click();
  await clearStaffing(page);
});

test("planning edits made during a pending save survive and cannot overlap saves", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await openAdvanced(page);
  await expect(page.getByLabel("Staffed start 1")).toHaveCount(0);
  let release!: () => void;
  let received!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const pending = new Promise<void>((resolve) => {
    received = resolve;
  });
  let writes = 0;
  await page.route("**/v1/admin/optimization", async (route) => {
    if (route.request().method() !== "PUT" || ++writes !== 1)
      return route.continue();
    const response = await route.fetch();
    received();
    await gate;
    await route.fulfill({ response });
  });
  const save = page.getByRole("button", { name: "Save planning assumptions" });
  await page.getByRole("button", { name: "Add staffed period" }).click();
  await save.click();
  await pending;
  try {
    await page.getByRole("button", { name: "Remove staffed period 1" }).click();
    await expect(save).toBeDisabled();
  } finally {
    release();
  }
  await expect(save).toBeEnabled();
  await expect(page.getByLabel("Staffed start 1")).toHaveCount(0);
  await clearStaffing(page);
  await page.reload();
  await openAdvanced(page);
  await expect(page.getByLabel("Staffed start 1")).toHaveCount(0);
});

/** Saves with no dated periods and waits for the server, so later tests in the shared
 * workspace do not inherit one (an earlier "saved" message is still on screen). */
async function clearStaffing(page: import("@playwright/test").Page) {
  const saved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/optimization") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save planning assumptions" }).click();
  const response = await saved;
  expect(response.ok()).toBeTruthy();
  expect(response.request().postDataJSON().staffed_windows).toEqual([]);
}

/** Dated periods and planning assumptions live in Settings → Advanced. */
async function openAdvanced(page: import("@playwright/test").Page) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Advanced", { exact: true }).click();
}
