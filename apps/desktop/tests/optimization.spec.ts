import { expect, test } from "./fixtures";
import { importLead, chooseOutcome } from "./simulation";

async function simulation(page: import("@playwright/test").Page) {
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
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
  if (await page.getByRole("button", { name: "Pause dialing" }).isVisible())
    await page.getByRole("button", { name: "Pause dialing" }).click();
}

test("missing and stale capacity estimates are explicit", async ({ page }) => {
  await simulation(page);
  let state = "pending";
  await page.route("**/v1/admin/numbers/summary", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.forecast = {
      regions: [],
      capacity_shortfall: null,
      staffing_shortfall: null,
      number_shortfall: null,
      projection: { status: state, as_of: null },
    };
    await route.fulfill({ response, json: body });
  });
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await expect(
    page.getByText(
      "No capacity plan yet. Plan regional capacity to prepare one.",
      { exact: true },
    ),
  ).toBeVisible();
  state = "stale";
  await page.reload();
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await expect(
    page.getByText(
      "Capacity plan is out of date. Plan regional capacity before acting on it.",
      { exact: true },
    ),
  ).toBeVisible();
});

test("capacity discloses screening exclusions without spending on refresh", async ({
  page,
}) => {
  await simulation(page);
  const paidRequests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /screen|registry/.test(request.url()))
      paidRequests.push(request.url());
  });
  await page.route("**/v1/admin/numbers/summary", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.forecast = {
      regions: [],
      forecast_attempts: 3,
      staffing_shortfall: 0,
      number_shortfall: 0,
      projection: { status: "ready", as_of: "2026-10-14T18:00:00Z" },
      screening: {
        cleared_count: 1,
        blocked_count: 2,
        blocked_reasons: { prepaid_result_missing: 1, source_expired: 1 },
        as_of: "2026-10-14T18:00:00Z",
      },
    };
    await route.fulfill({ response, json: body });
  });
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  const capacity = page.getByRole("region", {
    name: "Capacity before procurement",
  });
  await expect(
    capacity.getByText(
      "1 lead has current screening; 2 are excluded from executable capacity.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    capacity.getByText("Prepaid result missing: 1", { exact: true }),
  ).toBeVisible();
  await expect(
    capacity.getByText("Source expired: 1", { exact: true }),
  ).toBeVisible();
  await expect(
    capacity.getByText(
      /Review source authorization and current coverage in Settings/,
    ),
  ).toBeVisible();
  expect(paidRequests).toEqual([]);
});

test("pending report refresh preserves comparison input", async ({ page }) => {
  await simulation(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Advanced", { exact: true }).click();
  await page
    .getByLabel("Experiment review reference")
    .fill("Retain this draft during refresh");
  await page.route("**/v1/optimization", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      response,
      json: {
        settings: body.settings,
        projection: {
          status: "pending",
          as_of: null,
          refresh_status: "queued",
        },
      },
    });
  });
  // Saving planning assumptions reloads the shared report while a draft is open.
  await page.getByRole("button", { name: "Save planning assumptions" }).click();
  await expect(
    page.getByText("Preparing the first report in the background.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByLabel("Experiment review reference")).toHaveValue(
    "Retain this draft during refresh",
  );
  // Wait for an in-flight fixture handler before real reads resume.
  await page.unrouteAll({ behavior: "wait" });
  // A background refresh can read the real report first and retire this button; that is the
  // same refresh the click asks for, so the draft must survive it either way.
  const check = page.getByRole("button", {
    name: "Check report status",
    exact: true,
  });
  await check.click({ timeout: 5_000 }).catch(async (error) => {
    if (await check.isVisible()) throw error;
  });
  await expect(page.getByLabel("Experiment review reference")).toHaveValue(
    "Retain this draft during refresh",
  );
});

test("optimization goals, forecast, learning state and provider settings persist", async ({
  page,
}) => {
  await simulation(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByTestId("setup-campaign").locator(":scope > summary").click();
  await page.getByLabel("Conversion goal").selectOption("sale_won");
  await page.getByLabel("Outcome window (days)").fill("30");
  await page.getByRole("button", { name: "Save campaign goal" }).click();
  await page
    .getByRole("button", { name: "Apply reviewed goal change" })
    .click();
  await expect(page.getByText("Campaign goal saved").first()).toBeVisible();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByText("Stats & statistical learning", { exact: true }).click();
  await page
    .getByRole("button", { name: "Refresh learning and capacity" })
    .click();
  await expect(
    page.getByText("Future calling opportunities", { exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("saved-goal")).toContainText("Sale won");
  // Latency breakdowns are diagnostics that no longer sit in the normal view.
  await expect(page.getByText(/p95 inference/)).toHaveCount(0);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Advanced", { exact: true }).click();
  await expect(
    page.getByText("Operational training dataset", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Experiment type")).toHaveValue("model_pilot");
  await page.getByLabel("Experiment type").selectOption("coaching");
  await page
    .getByLabel("Experiment review reference")
    .fill("Synthetic protocol review");
  await page
    .getByRole("button", { name: "Start coaching comparison", exact: true })
    .click();
  await expect(
    page.getByText("Rep-balanced coaching", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Stop coaching comparison", exact: true })
    .click();
  await page.getByLabel("Experiment type").selectOption("model_pilot");
  await page.getByText("Train, stop, roll back or promote a candidate").click();
  await page
    .getByRole("button", { name: "Generate trial report", exact: true })
    .click();
  await expect(page.getByText(/Saved cohort report:/)).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByTestId("setup-campaign").locator(":scope > summary").click();
  await expect(page.getByLabel("Conversion goal")).toHaveValue("sale_won");
  await page.getByTestId("setup-helper").locator(":scope > summary").click();
  await page.getByText("Live coaching model", { exact: true }).click();
  await page.getByLabel("Live coach provider").selectOption("openrouter");
  await expect(
    page
      .getByRole("region", { name: "Optional model routes" })
      .getByText(/Cerebras only/),
  ).toBeVisible();
  // The key and route now share the same setup step.
  await expect(page.getByLabel("OpenRouter API key")).toBeVisible();
});

test("live events deliver cached guidance and conversion recording without call polling", async ({
  page,
}) => {
  await simulation(page);
  const sockets: string[] = [];
  page.on("websocket", (ws) => sockets.push(ws.url()));
  await page.reload();
  await expect(page.getByRole("button", { name: "Go ready" })).toBeVisible();
  await importLead(page, "Live events fixture", "+14155550706");
  await page.getByRole("button", { name: "Go ready" }).click();
  await page.getByRole("button", { name: "Get next lead" }).click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Simulate human answer" }).click();
  await page
    .getByLabel("Simulation transcript")
    .fill("The budget is not approved.");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  await expect(
    page
      .getByTestId("live-coaching-strip")
      .getByText("What outcome would make the investment worthwhile?", {
        exact: false,
      }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Private help", exact: true }).click();
  await expect(
    page.getByText("Seller-approved example · AI unavailable"),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Guidance was useful", exact: true })
    .click();
  await expect(
    page.getByText("Guidance feedback saved", { exact: true }),
  ).toBeVisible();
  expect(sockets.some((url) => url.endsWith("/v1/live"))).toBeTruthy();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "qualified_conversation");
  await page.getByRole("button", { name: "Save outcome" }).click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: /Review call/ })
    .first()
    .click();
  await page.getByLabel("Conversion event").selectOption("meeting_held");
  await page
    .getByLabel("Conversion source reference")
    .fill("Reviewed permissioned simulation meeting");
  await page.getByRole("button", { name: "Record conversion" }).click();
  await expect(
    page.getByText("Conversion recorded", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Conversion correction reason")
    .fill("Meeting entry was a simulation mistake");
  await page
    .getByRole("button", { name: "Retract conversion", exact: true })
    .first()
    .click();
  await expect(
    page.getByText("Conversion retracted", { exact: true }),
  ).toBeVisible();
});
