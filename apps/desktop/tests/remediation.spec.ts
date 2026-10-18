import { expect, test, type Page } from "@playwright/test";

async function enter(page: Page, connected = false) {
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
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
}

for (const connected of [false, true]) {
  test(`fixed proactive coaching, script, statistical brief and local wrapup (${connected ? "connected" : "local"})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1028, height: 749 });
    await enter(page, connected);
    if (await page.getByRole("button", { name: "Pause dialing" }).isVisible())
      await page.getByRole("button", { name: "Pause dialing" }).click();
    await page.getByRole("button", { name: "Go ready" }).click();
    await page.getByRole("button", { name: "Get next lead" }).click();
    await expect(page.getByTestId("prospect-brief")).toBeVisible();
    await expect(page.getByTestId("statistical-guidance")).toBeVisible();
    await expect(page.getByTestId("prepared-script")).toBeVisible();
    await page.getByRole("button", { name: "Start call", exact: true }).click();
    await page.getByRole("button", { name: "Simulate human answer" }).click();
    await page
      .getByLabel("Simulation transcript")
      .fill("Our approvals depend on a union committee.");
    await page.getByRole("button", { name: "Add prospect turn" }).click();
    const strip = page.getByTestId("live-coaching-strip");
    await expect(strip).toBeVisible();
    await page.mouse.wheel(0, 1200);
    const bounds = await strip.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThan(400);
    await expect(
      page.getByRole("button", { name: "Hang up", exact: true }),
    ).toBeInViewport();
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    await page.getByLabel("Disposition").selectOption("meeting_booked");
    await page.getByRole("button", { name: "Save outcome" }).click();
    await expect(page.getByText(/Meeting recorded.*rep/i)).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: /create calendar|send invitation|sync CRM/i,
      }),
    ).toHaveCount(0);
  });
}

test("no-subscription costs, short retention and prepaid DNC configuration are explicit", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByText(/five minutes|5 minutes/i).first()).toBeVisible();
  await expect(page.getByText(/No subscriptions/i).first()).toBeVisible();
  await expect(page.getByLabel("Prepaid DNC API key")).toBeVisible();
  await expect(page.getByText(/seller.*authorization/i).first()).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: /enable reputation subscription|connect calendar/i,
    }),
  ).toHaveCount(0);
});

test("batch script preparation exposes gatekeeper and screening sections", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await expect(page.getByTestId("preparation-runway")).toBeVisible();
  await page.getByRole("button", { name: "Prepare lead packets" }).click();
  await expect(page.getByText(/Preparation queued/)).toBeVisible();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await expect(page.getByText(/Gatekeeper/i).first()).toBeVisible();
  await expect(page.getByText(/iPhone screening/i).first()).toBeVisible();
});
