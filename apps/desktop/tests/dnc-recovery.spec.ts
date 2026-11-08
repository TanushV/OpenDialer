import { expect, test, type Page, hosted, HOSTED_ONLY } from "./fixtures";
import { openMoreSettings } from "./navigation";

async function enter(page: Page, connected: boolean) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  await expect(
    page
      .getByTitle("Sign out")
      .or(page.getByRole("button", { name: "Try a simulated workspace" })),
  ).toBeVisible();
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
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
}

for (const connected of [false]) {
  test.describe(
    connected ? "PostgreSQL connected screening" : "Local screening",
    () => {
      test("account DNC rates preserve provenance and show the dated reference separately", async ({
        page,
      }) => {
        await enter(page, connected);
        await openMoreSettings(page, "Privacy and spending rules");
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
        await openMoreSettings(page, "Privacy and spending rules");
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
        await openMoreSettings(page, "Privacy and spending rules");
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
        await openMoreSettings(page, "Privacy and spending rules");
        await expect(panel).toContainText("Not listed in this national check");
        expect(posts).toHaveLength(2);
      });
    },
  );
}
