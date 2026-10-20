import { expect, test, type Page } from "./fixtures";

async function enter(page: Page, connected: boolean) {
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
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "wait" });
});

for (const connected of [false, true]) {
  test.describe(
    connected ? "PostgreSQL connected screening" : "Local screening",
    () => {
      test("registry publication retains identity after lost response and reload", async ({
        page,
      }) => {
        const bodies: Record<string, unknown>[] = [];
        await page.route("**/v1/admin/registry/import", async (route) => {
          bodies.push(route.request().postDataJSON());
          if (bodies.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(200);
            return route.abort("failed");
          }
          return route.continue();
        });
        async function fill() {
          await page
            .getByRole("button", { name: "Settings", exact: true })
            .click();
          await page
            .getByRole("button", { name: "Policy & DNC", exact: true })
            .click();
          await page
            .getByLabel("Authorized area codes (comma separated)")
            .fill("415, 212, 512");
          await page
            .getByLabel("Authorization reference")
            .fill("Synthetic registry authority");
          await page.getByLabel("Dataset refreshed on").fill("2026-10-14");
          await page
            .getByLabel("Source access expires at")
            .fill("2026-10-20T12:00");
          await page
            .getByLabel("Listed E.164 numbers (one per line)")
            .fill("+14155550199");
        }
        await enter(page, connected);
        await fill();
        await page
          .getByRole("button", { name: "Validate & publish dataset" })
          .click();
        await expect.poll(() => bodies.length).toBe(1);
        await expect(
          page.getByText(/Failed to fetch|Load failed/),
        ).toBeVisible();
        await page.reload();
        await fill();
        await page
          .getByRole("button", { name: "Validate & publish dataset" })
          .click();
        await expect(
          page.getByText("Validated registry snapshot published atomically.", {
            exact: true,
          }),
        ).toBeVisible();
        expect(bodies).toHaveLength(2);
        expect(bodies[0].request_id).toBeTruthy();
        expect(bodies[1]).toEqual(bodies[0]);
        await page.getByLabel("Dataset import mode").selectOption("changes");
        await page.getByLabel("Base publication").selectOption({ index: 1 });
        await page
          .getByLabel("Added listed E.164 numbers")
          .fill("+14155550198");
        await page
          .getByLabel("Removed listed E.164 numbers")
          .fill("+14155550199");
        await page
          .getByRole("button", { name: "Validate & publish dataset" })
          .click();
        await expect.poll(() => bodies.length).toBe(3);
        expect(bodies[2].base_snapshot_id).toBeTruthy();
        expect(bodies[2].expected_version).toBeGreaterThan(0);
        expect(bodies[2].numbers).toBeUndefined();
        await expect(
          page.getByText("Validated registry snapshot published atomically.", {
            exact: true,
          }),
        ).toBeVisible();
      });

      test("account DNC rates preserve provenance and show the dated reference separately", async ({
        page,
      }) => {
        await enter(page, connected);
        await page
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        await page
          .getByRole("button", { name: "Policy & DNC", exact: true })
          .click();
        await page.getByText("Account rate estimates", { exact: true }).click();
        await page.getByLabel("Rate component").selectOption("dnc");
        await page.getByLabel("Estimated per query usd").fill("0.003");
        await page
          .getByLabel("Rate invoice or quote reference")
          .fill("Synthetic invoice reference");
        await page.getByLabel("Account rate checked on").fill("2026-10-14");
        await page
          .getByRole("button", { name: "Save account rate estimate" })
          .click();
        await expect(
          page.getByText("Account rate estimate saved", { exact: true }),
        ).toBeVisible();
        await page.reload();
        await page
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        await page
          .getByRole("button", { name: "Policy & DNC", exact: true })
          .click();
        await page.getByText("Account rate estimates", { exact: true }).click();
        await expect(page.getByLabel("Estimated per query usd")).toHaveValue(
          "0.003",
        );
        await expect(
          page.getByLabel("Rate invoice or quote reference"),
        ).toHaveValue("Synthetic invoice reference");
        await expect(
          page.getByText(
            "Published reference rates remain separate from account estimates and actual charges.",
            { exact: true },
          ),
        ).toBeVisible();
      });

      test("scheduled prepaid screening requires explicit budgets and saves without charged reads", async ({
        page,
      }) => {
        let charges = 0;
        await page.route("**/v1/admin/dnc/screen", (route) => {
          charges++;
          return route.fulfill({ json: { results: [] } });
        });
        await enter(page, connected);
        await page
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        await page
          .getByRole("button", { name: "Policy & DNC", exact: true })
          .click();
        const enabled = page.getByLabel(
          "Authorize scheduled prepaid DNC checks",
        );
        await expect(enabled).not.toBeChecked();
        await enabled.check();
        await page.getByLabel("DNC maximum per run (USD)").fill("0.05");
        await page.getByLabel("DNC maximum per day (USD)").fill("0.10");
        await page
          .getByRole("button", { name: "Save privacy and usage policy" })
          .click();
        await expect(
          page.getByText("Privacy and usage policy saved", { exact: true }),
        ).toBeVisible();
        await page.reload();
        await page
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        await page
          .getByRole("button", { name: "Policy & DNC", exact: true })
          .click();
        await expect(enabled).toBeChecked();
        await expect(page.getByLabel("DNC maximum per run (USD)")).toHaveValue(
          "0.05",
        );
        expect(charges).toBe(0);
        await enabled.uncheck();
        await page
          .getByRole("button", { name: "Save privacy and usage policy" })
          .click();
        await expect(
          page.getByText("Privacy and usage policy saved", { exact: true }),
        ).toBeVisible();
      });

      test("prepaid screening recovery needs a charge choice and preserves retry identity", async ({
        page,
      }) => {
        let completed = false;
        const posts: unknown[] = [];
        await page.route("**/v1/admin/dnc/screens", (route) =>
          route.fulfill({
            json: {
              screens: [
                {
                  id: "uncertain-screen",
                  version: 4,
                  phone: "+14155550101",
                  status: completed ? "completed" : "uncertain",
                  listed: completed ? false : null,
                  can_retry: !completed,
                  possible_charge_usd: 0.001667,
                  reason:
                    "Lookup result was lost; a previous charge may remain.",
                },
              ],
              limited: false,
            },
          }),
        );
        await page.route(
          "**/v1/admin/dnc/screens/uncertain-screen/retry",
          async (route) => {
            const body = route.request().postDataJSON();
            posts.push(body);
            expect(body.expected_version).toBe(4);
            expect(body.accept_possible_duplicate_charge).toBe(true);
            expect(body.idempotency_key).toBeTruthy();
            if (posts.length === 1)
              await route.fulfill({
                status: 503,
                json: { detail: "Screening unavailable" },
              });
            else {
              completed = true;
              await route.fulfill({ json: { status: "completed" } });
            }
          },
        );
        await enter(page, connected);
        await page
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        await page
          .getByRole("button", { name: "Policy & DNC", exact: true })
          .click();
        const panel = page.getByRole("region", {
          name: "Prepaid screening results",
          exact: true,
        });
        await expect(panel).toContainText("previous charge may remain");
        expect(posts).toHaveLength(0);
        const retry = panel.getByRole("button", {
          name: "Retry prepaid check",
          exact: true,
        });
        await expect(retry).toBeDisabled();
        await panel
          .getByLabel("I accept that another query may charge another credit")
          .check();
        await retry.click();
        await expect(panel).toContainText("Screening unavailable");
        await expect(retry).toBeEnabled();
        await retry.click();
        await expect(panel).toContainText("Not listed in this national check");
        await expect(retry).toHaveCount(0);
        expect(posts).toHaveLength(2);
        expect(posts[1]).toEqual(posts[0]);
        await page.reload();
        await page
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        await page
          .getByRole("button", { name: "Policy & DNC", exact: true })
          .click();
        await expect(panel).toContainText("Not listed in this national check");
        expect(posts).toHaveLength(2);
      });
    },
  );
}
