import { expect, test, hosted, HOSTED_ONLY } from "./fixtures";
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
});

for (const connected of [false]) {
  test(`${connected ? "connected" : "local"} number review recovers a stale edit and saves evidence policy`, async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    if (connected) {
      if (await page.getByTitle("Sign out").isVisible())
        await page.getByTitle("Sign out").click();
      await page.getByRole("button", { name: "Connect to a server" }).click();
      test.skip(!hosted, HOSTED_ONLY);
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
    await openMoreSettings(page, "Learning experiments and trackers");
    await page
      .getByText("Advanced number evidence policy", { exact: true })
      .click();
    await page.getByLabel("Health observation window (days)").fill("17");
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
    await openMoreSettings(page, "Learning experiments and trackers");
    await page
      .getByText("Advanced number evidence policy", { exact: true })
      .click();
    await expect(
      page.getByLabel("Health observation window (days)"),
    ).toHaveValue("17");
  });
}

test("numbers already in Telnyx are listed and added one at a time", async ({
  page,
}) => {
  const added: unknown[] = [];
  await page.route("**/v1/admin/numbers/sync", (route) =>
    route.fulfill({
      json: {
        owned: [
          { phone: "+16282564157", added: false, route: "unassigned" },
          { phone: "+19293799649", added: false, route: "other_app" },
          { phone: "+14155550100", added: true, route: "this_app" },
        ],
      },
    }),
  );
  await page.route("**/v1/admin/numbers/add", (route) => {
    added.push(route.request().postDataJSON());
    return route.fulfill({ json: {} });
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
  await expect(page.getByLabel("Existing owned phone number")).toHaveCount(0);
  await page.getByRole("button", { name: "Check my Telnyx numbers" }).click();
  const list = page.getByRole("group", {
    name: "Numbers in your Telnyx account",
  });
  await expect(list).not.toContainText("+14155550100");
  await expect(list).toContainText("On another Telnyx app");
  await list.getByRole("button", { name: "Add +16282564157" }).click();
  await list.getByRole("button", { name: "Move and add +19293799649" }).click();
  await expect.poll(() => added.length).toBe(2);
  expect(added).toEqual([
    { phone: "+16282564157", move_from_other_app: false },
    { phone: "+19293799649", move_from_other_app: true },
  ]);
});

test("number search names a city and shows where each number is", async ({
  page,
}) => {
  const searches: unknown[] = [];
  await page.route("**/v1/admin/numbers/search", (route) => {
    searches.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        numbers: [
          {
            id: "quote-city",
            phone: "+16282564157",
            region: "628",
            area_code: "628",
            city: "San Francisco",
            initial_commitment_usd: 2,
            monthly_cost_usd: 1,
            expires_at: "2026-11-04T23:59:00Z",
          },
        ],
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
  await page.getByText("Add numbers with approval", { exact: true }).click();
  await page.getByLabel("Area code", { exact: true }).fill("628");
  await page.getByLabel("City (optional)").fill("San Francisco");
  await page.getByRole("button", { name: "Find available numbers" }).click();
  await expect(page.getByText(/\+16282564157 · San Francisco/)).toBeVisible();
  expect(searches).toEqual([{ region: "628", city: "San Francisco" }]);
});

test("registry export uses the native save command in the desktop", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const simulate = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await simulate.isVisible()) await simulate.click();
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  const exportButton = page.getByRole("button", {
    name: "Export registry CSV",
  });
  await expect(exportButton).toBeVisible();
  await page.evaluate(() => {
    Object.assign(window, {
      __TAURI_INTERNALS__: {
        invoke: async (command: string, args: unknown) => {
          if (command === "save_registry_csv") {
            document.documentElement.dataset.nativeExport =
              JSON.stringify(args);
            return "/test/Exports/opendialer-caller-registry.csv";
          }
        },
      },
    });
  });
  await exportButton.click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-native-export",
    /phone_number/,
  );
  await expect(
    page.getByText(/Registry CSV saved to.*opendialer-caller-registry\.csv/),
  ).toBeVisible();
});
