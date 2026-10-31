import { expect, test, type Page } from "./fixtures";

for (const connected of [false, true]) {
  test(`Codex file import and disconnect (${connected ? "hosted" : "local"})`, async ({
    page,
  }) => {
    let imported = false;
    await page.route("**/v1/inference/codex", async (route) => {
      if (route.request().method() === "DELETE") imported = false;
      await route.fulfill({
        json: {
          connected: imported,
          account: imported
            ? { email: "fixture@example.test", planType: "pro" }
            : null,
        },
      });
    });
    await page.route("**/v1/inference/codex/import", async (route) => {
      expect(route.request().postDataJSON().tokens.refresh_token).toBe(
        "fixture-refresh",
      );
      imported = true;
      await route.fulfill({
        json: {
          connected: true,
          account: { email: "fixture@example.test", planType: "pro" },
        },
      });
    });
    await enter(page, connected);
    await page.getByLabel("Import Codex login").setInputFiles({
      name: "invalid.json",
      mimeType: "application/json",
      buffer: Buffer.from("not JSON"),
    });
    await expect(
      page.getByRole("region", { name: "Your Codex login" }).getByRole("alert"),
    ).toContainText("not a JSON login file");
    expect(imported).toBe(false);
    await page.getByLabel("Import Codex login").setInputFiles({
      name: "auth.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({ tokens: { refresh_token: "fixture-refresh" } }),
      ),
    });
    await expect(
      page.getByText(/Connected as fixture@example.test/),
    ).toBeVisible();
    await expect(
      page.getByText(/GPT-6.1 Terra at maximum effort/),
    ).toBeVisible();
    expect(
      await page.evaluate(() => JSON.stringify(localStorage)),
    ).not.toContain("fixture-refresh");
    await page.getByRole("button", { name: "Disconnect ChatGPT" }).click();
    await expect(page.getByLabel("Import Codex login")).toBeVisible();
  });
}

test("a late status read cannot erase a successful Codex import", async ({
  page,
}) => {
  let release = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/inference/codex", async (route) => {
    await held;
    await route.fulfill({ json: { connected: false, account: null } });
  });
  await page.route("**/v1/inference/codex/import", (route) =>
    route.fulfill({
      json: {
        connected: true,
        account: { email: "late@example.test", planType: "pro" },
      },
    }),
  );
  const pending = page.waitForRequest((request) =>
    request.url().endsWith("/v1/inference/codex"),
  );
  await enter(page, false);
  await pending;
  await page.getByLabel("Import Codex login").setInputFiles({
    name: "auth.json",
    mimeType: "application/json",
    buffer: Buffer.from("{}"),
  });
  await expect(page.getByText(/Connected as late@example.test/)).toBeVisible();
  const response = page.waitForResponse((value) =>
    value.url().endsWith("/v1/inference/codex"),
  );
  release();
  await (await response).finished();
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  await expect(page.getByText(/Connected as late@example.test/)).toBeVisible();
});

/** Local workspace, or the connected PostgreSQL authority of the e2e harness. */
async function enter(page: Page, connected: boolean) {
  // The sign-in page would be a real OpenAI page: record it instead of opening it.
  await page.addInitScript(() => {
    window.open = ((url?: string | URL) => {
      (window as unknown as { opened: string }).opened = String(url);
      return null;
    }) as typeof window.open;
  });
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByTestId("setting-AI")).toBeVisible();
}
