import { expect, test } from "./fixtures";
import { openMoreSettings, openSettingsCard } from "./navigation";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

async function enter(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const choice = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await choice.isVisible()) await choice.click();
  await expect(
    page.getByRole("button", { name: "Leads", exact: true }),
  ).toBeVisible();
}

test("app updates explain the 0.4.0 fresh workspace", async ({ page }) => {
  await enter(page);
  await openMoreSettings(page, "Workspace and app updates");
  await expect(page.getByText(/0.4.0 starts a fresh workspace/)).toBeVisible();
  await expect(page.getByText(/old data stays untouched/)).toBeVisible();
});

test("home metrics show pending and stale evidence without invented zeros", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.metrics = { projection: { status: "pending", as_of: null } };
    await route.fulfill({ response, json: data });
  });
  await enter(page);
  await expect(
    page.getByText("Preparing workspace totals in the background.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText("undefined", { exact: true })).toHaveCount(0);
  await expect(page.getByText("undefined currently eligible")).toHaveCount(0);
});

test("lead search continues beyond an empty bounded page without hiding older leads", async ({
  page,
}) => {
  let found = false;
  await page.route("**/v1/workspace/leads**", async (route) => {
    const url = new URL(route.request().url());
    if (!url.searchParams.get("q")) return route.continue();
    found = url.searchParams.has("cursor");
    await route.fulfill({
      json: {
        items: found
          ? [
              {
                id: "rare-id",
                name: "Rare Needle",
                company: "Archive Co",
                phone: "+12125550199",
                status: "eligible",
                attempt_count: 1,
              },
            ]
          : [],
        total: 900,
        next_cursor: found ? null : "search-next",
        search_complete: found,
        scanned: 200,
      },
    });
  });
  await enter(page);
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByRole("textbox", { name: "Search leads" }).fill("needle");
  await page
    .getByRole("button", { name: "Search lead history", exact: true })
    .click();
  await expect(
    page.getByText("More records remain to search.", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Continue lead search", exact: true })
    .click();
  await expect(page.getByText("Rare Needle", { exact: true })).toBeVisible();
  expect(found).toBe(true);
  await expect(
    page.getByRole("region", { name: "Lead preparation" }),
  ).toBeVisible();
});

test("call history loads older named calls independently of the workspace lead preview", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.calls = [
      {
        id: "new-call",
        lead_id: "new-person",
        lead_name: "Recent Person",
        lead_company: "New Co",
        state: "FINALIZED",
        outcome: "right_party",
      },
    ];
    data.pages = {
      ...data.pages,
      calls: { total: 101, next_cursor: "older-calls" },
    };
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/workspace/calls**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: "old-call",
            lead_id: "old-person",
            lead_name: "Older Person",
            lead_company: "Earlier Co",
            state: "FINALIZED",
            outcome: "callback_requested",
          },
        ],
        total: 101,
        next_cursor: null,
      },
    }),
  );
  await enter(page);
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await expect(
    page.getByRole("button", {
      name: "Review call Recent Person",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Load older calls", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Review call Older Person", exact: true }),
  ).toBeVisible();
});

test("registry history remains accessible beyond the workspace preview", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.pages = {
      ...data.pages,
      registries: { total: 51, next_cursor: "older-registry" },
    };
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/workspace/registries**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: "older-registry",
            kind: "state",
            npas: ["999"],
            refreshed_at: "2026-10-14T00:00:00Z",
            checksum: "older-snapshot-checksum",
          },
        ],
        total: 51,
        next_cursor: null,
      },
    }),
  );
  await enter(page);
  await openSettingsCard(page, "Do-not-call list");
  await page
    .getByRole("button", { name: "Load older registry snapshots", exact: true })
    .click();
  await expect(page.getByText(/older-snapshot/)).toBeVisible();
});

test("a newer searched lead is not overwritten by an older workspace preview", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.leads = [
      {
        id: "same-lead",
        version: 1,
        name: "Old Name",
        company: "Example",
        phone: "+12125550199",
        status: "eligible",
        attempt_count: 0,
      },
    ];
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/workspace/leads**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: "same-lead",
            version: 2,
            name: "Corrected Name",
            company: "Example",
            phone: "+12125550199",
            status: "eligible",
            attempt_count: 0,
          },
        ],
        total: 1,
        next_cursor: null,
      },
    }),
  );
  await enter(page);
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByRole("textbox", { name: "Search leads" }).fill("Corrected");
  await page
    .getByRole("button", { name: "Search lead history", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Open lead Corrected Name", exact: true }),
  ).toBeVisible();
});

test("failed workspace totals expose recovery even before the first result", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.calls = [];
    data.metrics = {
      projection: {
        status: "pending",
        refresh_status: "dead",
        refresh_error: "Workspace totals need recovery",
      },
    };
    await route.fulfill({ response, json: data });
  });
  await enter(page);
  await expect(
    page.getByText("Workspace totals need recovery", { exact: true }),
  ).toBeVisible();
});

test("editing an older follow-up keeps the returned version beyond the preview", async ({
  page,
}) => {
  const task = {
    id: "old-edit-task",
    version: 1,
    call_id: "old-edit-call",
    action: "Complete the old promise",
    status: "open",
    due_at: "2026-10-20T18:00:00Z",
  };
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.tasks = [];
    data.pages = {
      ...data.pages,
      tasks: { total: 100, next_cursor: "older-tasks" },
    };
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/workspace/tasks**", (route) =>
    route.fulfill({ json: { items: [task], total: 100, next_cursor: null } }),
  );
  await page.route("**/v1/tasks/old-edit-task", (route) =>
    route.fulfill({
      json: { ...task, ...route.request().postDataJSON(), version: 2 },
    }),
  );
  await enter(page);
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: "Load more follow-ups", exact: true })
    .click();
  const row = page.locator('[data-call-id="old-edit-call"]');
  await row.getByRole("button", { name: "Complete", exact: true }).click();
  await expect(row.getByText("Completed", { exact: true })).toBeVisible();
  await expect(
    row.getByRole("button", { name: "Complete", exact: true }),
  ).toBeDisabled();
});

test("editing a number beyond the preview immediately updates its lifecycle", async ({
  page,
}) => {
  const number = {
    id: "old-edit-number",
    version: 1,
    phone: "+12125550930",
    region: "general",
    lifecycle: "healthy",
    capacity: 1,
    daily_limit: 20,
  };
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.numbers = [];
    data.pages = {
      ...data.pages,
      numbers: { total: 100, next_cursor: "older-numbers" },
    };
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/workspace/numbers**", (route) =>
    route.fulfill({ json: { items: [number], total: 100, next_cursor: null } }),
  );
  await page.route("**/v1/admin/numbers/old-edit-number", (route) =>
    route.fulfill({
      json: { ...number, ...route.request().postDataJSON(), version: 2 },
    }),
  );
  await enter(page);
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await page
    .getByRole("button", { name: "Load more numbers", exact: true })
    .click();
  const row = page.getByRole("row").filter({ hasText: number.phone });
  await row
    .getByRole("button", { name: "Pause outbound", exact: true })
    .click();
  await expect(
    row.getByText("Paused for outbound", { exact: true }),
  ).toBeVisible();
});

test("failed call filtering keeps the previously displayed filter honest", async ({
  page,
}) => {
  await page.route("**/v1/workspace/calls**", (route) =>
    route.fulfill({
      status: 503,
      json: { detail: "History temporarily unavailable" },
    }),
  );
  await enter(page);
  await page.getByRole("button", { name: "Review", exact: true }).click();
  const filter = page.getByLabel(/^Filter/);
  await filter.selectOption("wrong_party");
  await expect(
    page.getByText("History temporarily unavailable", { exact: true }),
  ).toBeVisible();
  await expect(filter).toHaveValue("all");
});

for (const integrity of [null, false]) {
  test(`workspace indicator distinguishes unchecked integrity from ${integrity === null ? "failure" : "confirmed failure"}`, async ({
    page,
  }) => {
    await page.route("**/v1/workspace", async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      data.readiness.integrity_ok = integrity;
      await route.fulfill({ response, json: data });
    });
    await enter(page);
    const indicator = page.locator("header.topbar .connection .status-dot");
    if (integrity === false) await expect(indicator).toHaveClass(/danger/);
    else await expect(indicator).not.toHaveClass(/danger/);
  });
}
