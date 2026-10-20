import { expect, test, type Page } from "./fixtures";

const AUTHORITY = "http://127.0.0.1:8765";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

async function enter(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
}

async function openSettings(page: Page) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Setup checklist" }),
  ).toBeVisible();
}

/** Visible interactive controls in the page body: the first-screen budget. */
async function visibleControls(page: Page) {
  return await page
    .locator(
      ".page-body :is(button, input, select, textarea, summary, a[href])",
    )
    .evaluateAll(
      (nodes) =>
        // checkVisibility honours closed <details>, hidden pages and display:none.
        nodes.filter((node) =>
          (node as Element).checkVisibility({ visibilityProperty: true }),
        ).length,
    );
}

test("a rep sees only rep settings and never fires an administrator read", async ({
  page,
}) => {
  await enter(page);
  const owner = await (
    await page.request.post(`${AUTHORITY}/v1/local/session`, {
      headers: { "X-Local-Token": "e2e-local-token" },
    })
  ).json();
  const email = `setup-rep-${Date.now()}@example.test`;
  const created = await page.request.post(`${AUTHORITY}/v1/admin/users`, {
    headers: { authorization: `Bearer ${owner.access_token}` },
    data: {
      email,
      name: "Setup Rep",
      role: "rep",
      password: "rep-setup-password-123",
    },
  });
  expect(created.ok()).toBeTruthy();
  await page.getByTitle("Sign out").click();
  const adminReads: string[] = [];
  const forbidden: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/v1/admin/")) adminReads.push(r.url());
  });
  page.on("response", (r) => {
    if (r.status() === 403) forbidden.push(r.url());
  });
  await page.getByRole("button", { name: "Connect to a server" }).click();
  await page.getByLabel("Server URL").fill(AUTHORITY);
  await page.getByLabel("Email").fill(email);
  await page
    .getByLabel("Password", { exact: true })
    .fill("rep-setup-password-123");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Numbers", exact: true }),
  ).toHaveCount(0);
  for (const view of ["Leads", "Review", "Intelligence", "Settings"]) {
    await page.getByRole("button", { name: view, exact: true }).click();
    await page.waitForLoadState("networkidle");
  }
  await expect(page.getByText("Working time & corrections")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Switch workspace or server" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Run preflight" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("button", { name: "Policy & DNC" })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await expect(page.getByRole("button", { name: "Import leads" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Prepare displayed lead packets" }),
  ).toHaveCount(0);
  expect(adminReads).toEqual([]);
  expect(forbidden).toEqual([]);
});

test("setup is a short checklist that reads cached capability evidence on open", async ({
  page,
}) => {
  await enter(page);
  await openSettings(page);
  const steps = page.locator(".setup-step");
  await expect(steps).toHaveCount(9);
  for (const id of [
    "identity",
    "telnyx",
    "helper",
    "dnc",
    "numbers",
    "leads",
    "campaign",
    "schedule",
    "budget",
  ])
    await expect(page.getByTestId(`setup-${id}`)).toBeVisible();
  // Steps open collapsed: the first screen stays within a small control budget.
  expect(await visibleControls(page)).toBeLessThanOrEqual(16);
  await expect(page.getByLabel("Call Control connection ID")).toBeHidden();
  await page.getByRole("button", { name: "Run preflight" }).click();
  await expect(page.getByTestId("capability-live_helper")).toContainText(
    "Software verified",
  );
  // A fresh open reads the authority's cached evidence without another preflight.
  const preflights: string[] = [];
  page.on("request", (r) => {
    if (r.url().endsWith("/v1/admin/preflight")) preflights.push(r.url());
  });
  await page.reload();
  await openSettings(page);
  await expect(page.getByTestId("capability-offline_callback")).toContainText(
    "Unavailable",
  );
  await expect(page.getByText(/Capability evidence checked/)).toBeVisible();
  expect(preflights).toEqual([]);
});

test("calling policy publication is version-fenced and reloads after a conflict", async ({
  page,
}) => {
  await enter(page);
  await openSettings(page);
  const bodies: Record<string, unknown>[] = [];
  let conflict = true;
  await page.route("**/v1/admin/policies/*/publish", async (route) => {
    bodies.push(route.request().postDataJSON());
    if (conflict) {
      conflict = false;
      await route.fulfill({
        status: 409,
        json: {
          detail:
            "POLICY_VERSION_CONFLICT: a newer calling policy is current; reload it before publishing",
        },
      });
      return;
    }
    await route.continue();
  });
  await page.getByTestId("setup-campaign").locator(":scope > summary").click();
  await expect(
    page.getByLabel("Require current national registry coverage"),
  ).toHaveCount(0);
  await page.getByLabel("Attempt limit").fill("3");
  await page.getByLabel("Publication reason").fill("Fewer attempts");
  await page
    .getByRole("button", { name: "Publish calling window", exact: true })
    .click();
  await expect(
    page.getByText(/A newer calling policy is current/).first(),
  ).toBeVisible();
  expect(bodies[0]).toMatchObject({
    max_attempts: 3,
    expected_version: expect.any(Number),
  });
  expect(bodies[0]).not.toHaveProperty("registry_required");
  await page
    .getByRole("button", { name: "Reload calling policy", exact: true })
    .click();
  await expect(page.getByLabel("Attempt limit")).not.toHaveValue("3");
  await page.getByLabel("Attempt limit").fill("3");
  await page.getByLabel("Publication reason").fill("Fewer attempts");
  const published = page.waitForResponse(
    (r) => r.url().endsWith("/publish") && r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Publish calling window", exact: true })
    .click();
  expect((await published).ok()).toBeTruthy();
  // The new version is current once the workspace refresh lands.
  await expect(
    page
      .getByText("New policy version published. Queued calls will recheck it.")
      .first(),
  ).toBeVisible();
  await expect(page.getByLabel("Attempt limit")).toHaveValue("3");
  // Restore the shared fixture policy for later journeys.
  await page.getByLabel("Attempt limit").fill("4");
  await page.getByLabel("Publication reason").fill("Restore fixture");
  const restored = page.waitForResponse(
    (r) => r.url().endsWith("/publish") && r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Publish calling window", exact: true })
    .click();
  expect((await restored).ok()).toBeTruthy();
});

test("form bounds come from the authority's definitions", async ({ page }) => {
  await page.route("**/v1/definitions", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.bounds.policy.max_attempts.max = 7;
    body.bounds.budget.daily_limit_usd.max = 55;
    await route.fulfill({ response, json: body });
  });
  await enter(page);
  await openSettings(page);
  await page.getByTestId("setup-campaign").locator(":scope > summary").click();
  await expect(page.getByLabel("Attempt limit")).toHaveAttribute("max", "7");
  await page.getByTestId("setup-budget").locator(":scope > summary").click();
  await expect(
    page.getByLabel("Daily optional-work limit (USD)"),
  ).toHaveAttribute("max", "55");
});

test("the weekly schedule and dated periods share one picked IANA zone", async ({
  page,
}) => {
  await enter(page);
  await openSettings(page);
  await page.getByTestId("setup-schedule").locator(":scope > summary").click();
  const zone = page.getByRole("combobox", { name: "Staffing time zone" });
  await zone.selectOption("America/New_York");
  const saved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/optimization") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save rep schedule" }).click();
  expect((await saved).ok()).toBeTruthy();
  await page.getByText("Advanced", { exact: true }).click();
  await page.getByRole("button", { name: "Add staffed period" }).click();
  await page.getByLabel("Staffed start 1").fill("2026-10-19T09:00");
  await page.getByLabel("Staffed end 1").fill("2026-10-19T12:00");
  const advanced = page.waitForRequest(
    (r) => r.method() === "PUT" && r.url().endsWith("/v1/admin/optimization"),
  );
  await page.getByRole("button", { name: "Save planning assumptions" }).click();
  const body = (await advanced).postDataJSON();
  // 09:00 in New York on 19 October is 13:00 UTC, whatever the device zone.
  expect(body.staffed_windows[0].start_at).toBe("2026-10-19T13:00:00.000Z");
  expect(body).not.toHaveProperty("conversion_goal");
  // Restore the shared fixture schedule.
  await page.getByRole("button", { name: "Remove staffed period 1" }).click();
  await page.getByRole("button", { name: "Save planning assumptions" }).click();
  await zone.selectOption("UTC");
  await page.getByRole("button", { name: "Save rep schedule" }).click();
  await expect(page.getByText("Rep schedule saved").first()).toBeVisible();
});

test("advanced detail is one collapsed view and debug controls are gone", async ({
  page,
}) => {
  const strategyReads: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/v1/optimization/strategies"))
      strategyReads.push(r.url());
  });
  await enter(page);
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  for (const gone of [
    "Process queued work",
    "Publish approved variant",
    "Reconcile records",
    "Evaluate reviewed replay",
  ])
    await expect(page.getByRole("button", { name: gone })).toHaveCount(0);
  await expect(page.getByLabel("Conversion goal")).toHaveCount(0);
  await openSettings(page);
  expect(strategyReads).toEqual([]);
  await page.getByText("Advanced", { exact: true }).click();
  const experiments = page.getByRole("region", { name: "Experiments" });
  await expect(experiments).toBeVisible();
  await expect(page.getByRole("region", { name: "Experiments" })).toHaveCount(
    1,
  );
  await expect.poll(() => strategyReads.length).toBeGreaterThan(0);
  await expect(page.getByLabel("Human pickup prior successes")).toHaveCount(0);
  await page.getByText("Sparse estimate assumptions", { exact: true }).click();
  await expect(page.getByTestId("estimate-assumptions")).toContainText(
    "Human pickup prior",
  );
  for (const gone of [
    "Reviewed replay cases JSON",
    "External reputation evidence",
    "Carrier record JSON",
  ])
    await expect(page.getByLabel(gone)).toHaveCount(0);
  await expect(
    page.getByRole("table", { name: "Rolling evaluation support" }),
  ).toHaveCount(0);
});

test("trial review and activation send only identities, never evidence", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const value = await response.json();
    if (value.models)
      value.models = [
        {
          id: "trial-fixture",
          checksum: "b".repeat(64),
          active: false,
          feature_schema_version: 2,
          reviewable: true,
          goal: "meeting_held",
          metrics: { brier: 0.2, pooled_baseline_brier: 0.21 },
        },
      ];
    await route.fulfill({ response, json: value });
  });
  const bodies: Record<string, Record<string, unknown>> = {};
  await page.route("**/v1/admin/models/trial-fixture/*", async (route) => {
    const action = route.request().url().split("/").pop()!;
    bodies[action] = route.request().postDataJSON();
    await route.fulfill({ json: { id: "trial-fixture", version: 2 } });
  });
  await enter(page);
  await openSettings(page);
  await page.getByText("Advanced", { exact: true }).click();
  const experiments = page.getByRole("region", { name: "Experiments" });
  await experiments
    .getByRole("combobox", { name: "Pilot candidate" })
    .selectOption("trial-fixture");
  await experiments
    .getByLabel("Experiment review reference")
    .fill("Reviewed cohort");
  await experiments
    .getByText("Train, stop, roll back or promote a candidate")
    .click();
  await experiments.getByLabel("Saved cohort report ID").fill("report-1");
  await experiments.getByLabel("Saved cohort report ID").fill("");
  await expect(experiments.getByLabel("Saved cohort report ID")).toHaveValue(
    "",
  );
  await experiments.getByLabel("Saved cohort report ID").fill("report-1");
  await experiments
    .getByRole("button", { name: "Review saved cohort evidence" })
    .click();
  await expect.poll(() => bodies.trial).toBeTruthy();
  expect(bodies.trial).toEqual({
    report_reference: "Reviewed cohort",
    cohort_report_id: "report-1",
  });
  await experiments
    .getByRole("button", { name: "Activate after gates" })
    .click();
  await expect.poll(() => bodies.activate).toBeTruthy();
  expect(bodies.activate).toEqual({});
});

test("numbers show whole-inventory counts and one capacity view without a planning loop", async ({
  page,
}) => {
  const proposals: string[] = [];
  page.on("request", (r) => {
    if (r.url().endsWith("/v1/admin/numbers/proposal"))
      proposals.push(r.method());
  });
  await page.route("**/v1/admin/numbers/summary", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.inventory = {
      ...body.inventory,
      total: 120,
      ready: 7,
      pending_setup: 11,
    };
    body.stable_assignments = 64;
    await route.fulfill({ response, json: body });
  });
  await enter(page);
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  const status = page.getByRole("region", { name: "Inventory status" });
  await expect(status.getByText("11", { exact: true })).toBeVisible();
  await expect(page.getByText("120", { exact: true })).toBeVisible();
  await expect(page.getByText("64", { exact: true })).toBeVisible();
  await expect(page.getByText("Stable", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Procurement area code")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Get carrier quotes" }),
  ).toHaveCount(0);
  await page.waitForTimeout(4500);
  expect(proposals).toEqual([]);
  await page
    .getByRole("button", { name: "Plan regional capacity", exact: true })
    .click();
  await expect(page.getByTestId("capacity-proposal")).toBeVisible({
    timeout: 20000,
  });
  expect(proposals).toEqual(["POST"]);
  await expect(
    page.getByRole("region", { name: "Capacity before procurement" }),
  ).toHaveCount(1);
  await expect(
    page.getByText(/minimum addition is a proposal only/),
  ).toBeVisible();
});

test("lead preparation previews its count and budget before queueing", async ({
  page,
}) => {
  const queued: string[] = [];
  page.on("request", (r) => {
    if (r.url().endsWith("/v1/research/batches") && r.method() === "POST")
      queued.push(r.url());
  });
  await enter(page);
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByRole("button", { name: "Prepare displayed lead packets" })
    .click();
  const preview = page.getByRole("region", { name: "Preparation preview" });
  await expect(preview).toContainText(/Prepare \d+ displayed lead/);
  expect(queued).toEqual([]);
  await preview.getByRole("button", { name: "Queue preparation" }).click();
  await expect(page.getByText(/Preparation queued/)).toBeVisible();
  expect(queued).toHaveLength(1);
});

test("disabled actions say why and settings failures stay recoverable", async ({
  page,
}) => {
  let failSettings = true;
  await page.route("**/v1/admin/settings", async (route) => {
    if (failSettings && route.request().method() === "GET") {
      await route.fulfill({
        status: 503,
        json: { detail: "Settings temporarily unavailable" },
      });
      return;
    }
    await route.continue();
  });
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.quiescence = { can_disconnect: false, reasons: ["active_call"] };
    await route.fulfill({ response, json: body });
  });
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByText(/Saved settings could not be loaded/),
  ).toBeVisible();
  failSettings = false;
  await page.getByRole("button", { name: "Retry loading settings" }).click();
  await expect(page.locator(".setup-step")).toHaveCount(9);
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await expect(page.getByText("Finish first: Active call.")).toBeVisible();
  await page.getByRole("button", { name: "Policy & DNC", exact: true }).click();
  await expect(page.getByLabel("Authorization reference")).toBeVisible();
  await expect(
    page.getByLabel("Subscription / authorization reference"),
  ).toHaveCount(0);
  await expect(
    page.getByText("Enter the authorized area codes.", { exact: true }),
  ).toBeVisible();
});
