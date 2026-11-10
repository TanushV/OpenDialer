import { expect, test } from "./fixtures";
import { openMoreSettings } from "./navigation";

async function enterSimulation(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
}

test("a number pauses and resumes from its own row, with no review form", async ({
  page,
}) => {
  // R10.7: each number shows whether it can dial, its calls today against its cap, and one control.
  await enterSimulation(page);
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  const ready = page
    .locator("tr")
    .filter({ has: page.getByText("Ready to dial", { exact: true }) })
    .first();
  const phone = (await ready.locator("strong").textContent())!;
  const row = page.locator("tr").filter({ hasText: phone });
  await expect(row).toContainText(/\d+ today · \d+ max/);
  await row.getByRole("button", { name: "Pause outbound" }).click();
  await expect(row.getByText("Paused", { exact: true })).toBeVisible();
  await row.getByRole("button", { name: "Resume", exact: true }).click();
  await expect(row.getByText("Ready to dial", { exact: true })).toBeVisible();
  for (const gone of ["Review number evidence", "Apply number review"])
    await expect(page.getByRole("button", { name: gone })).toHaveCount(0);
});

test("the number health window saves under Advanced", async ({ page }) => {
  await enterSimulation(page);
  await openMoreSettings(page, "Number limits");
  await page
    .getByText("Advanced number evidence policy", { exact: true })
    .click();
  await page.getByLabel("Health observation window (days)").fill("17");
  const policy = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/optimization") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save number limits" }).click();
  expect((await policy).ok()).toBeTruthy();
  await page.reload();
  await openMoreSettings(page, "Number limits");
  await page
    .getByText("Advanced number evidence policy", { exact: true })
    .click();
  await expect(page.getByLabel("Health observation window (days)")).toHaveValue(
    "17",
  );
});

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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
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
