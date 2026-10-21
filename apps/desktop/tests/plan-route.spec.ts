import { expect, test, type Page } from "./fixtures";

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
  await page.getByText("Advanced", { exact: true }).click();
  await page
    .getByLabel("Preparation and analysis route")
    .selectOption("chatgpt_plan");
}

test("hosted ChatGPT plan sign-in completes from the pasted loopback address", async ({
  page,
}) => {
  await enter(page, true);
  await page.getByRole("button", { name: "Continue with ChatGPT" }).click();
  await expect(page.getByText(/paste the address it ends on/)).toBeVisible();
  const opened = await page.evaluate(
    () => (window as unknown as { opened: string }).opened,
  );
  const authorize = new URL(opened);
  expect(authorize.origin).toBe("https://auth.openai.com");
  expect(authorize.searchParams.get("redirect_uri")).toBe(
    "http://127.0.0.1:1455/auth/callback",
  );
  // The code exchange itself is the server's; its contract is covered by pytest.
  let pasted = "";
  await page.route("**/v1/inference/chatgpt/complete", async (route) => {
    pasted = route.request().postDataJSON().redirect_url;
    await route.fulfill({ json: { connected: true } });
  });
  const address = `http://127.0.0.1:1455/auth/callback?code=one-time&state=${authorize.searchParams.get("state")}`;
  await page.getByLabel("Address shown after signing in").fill(address);
  await page.getByRole("button", { name: "Finish ChatGPT sign-in" }).click();
  await expect(
    page.getByText("ChatGPT is connected to your OpenDialer user."),
  ).toBeVisible();
  expect(pasted).toBe(address);
  await expect(page.getByLabel("Address shown after signing in")).toHaveCount(
    0,
  );
});

test("local ChatGPT plan sign-in returns through the local authority", async ({
  page,
}) => {
  await enter(page, false);
  await page.getByRole("button", { name: "Continue with ChatGPT" }).click();
  await expect(
    page.getByText(/Complete the official authorization in your browser/),
  ).toBeVisible();
  await expect(page.getByLabel("Address shown after signing in")).toHaveCount(
    0,
  );
});
