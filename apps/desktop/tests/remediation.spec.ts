import { expect, test, type Page } from "./fixtures";
import { openMoreSettings, openSettingsCard } from "./navigation";
import {
  chooseOutcome,
  importLead,
  isolatedSimulationWorkspace,
  waitForPreparation,
} from "./simulation";

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
    await importLead(
      page,
      `Proactive coaching fixture ${connected ? "connected" : "local"}`,
      connected ? "+14155550708" : "+14155550707",
    );
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
    await chooseOutcome(page, "meeting_booked");
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
  await openMoreSettings(page, "Privacy and spending rules");
  await expect(
    page.getByText("Raw calls and transcripts: five minutes at most.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText(/Spending permissions/i).first()).toBeVisible();
  await expect(page.getByLabel("Prepaid DNC API key")).toBeVisible();
  await expect(page.getByText(/seller.*authorization/i).first()).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: /enable reputation subscription|connect calendar/i,
    }),
  ).toHaveCount(0);
});

test("a requested lead's own script shows its gatekeeper and screening sections", async ({
  page,
}) => {
  await enter(page);
  // Earlier specs replace the shared workspace's script; use the seeded one.
  await isolatedSimulationWorkspace(page, "gatekeeper-script");
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await waitForPreparation(page);
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  const ready = page.getByRole("button", { name: "Go ready", exact: true });
  if (await ready.isVisible()) await ready.click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  const script = page.getByTestId("prepared-script");
  await expect(
    script.getByRole("heading", { name: "Gatekeeper", exact: true }),
  ).toBeVisible();
  await expect(
    script.getByRole("heading", { name: "Screening", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
});

test("a route choice made during a settings refresh is the one saved", async ({
  page,
}) => {
  await enter(page);
  const card = await openSettingsCard(page, "AI");
  await card.getByText("Use an OpenAI API key instead of ChatGPT").click();
  const route = card.getByLabel("Research and scripts use");
  await expect(route).toBeVisible();
  // Hold a settings refresh until after the choice.
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/v1/admin/settings", async (request) => {
    if (request.request().method() === "GET") await held;
    await request.continue();
  });
  const loaded = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/settings") && r.request().method() === "GET",
  );
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await route.selectOption("api");
  release();
  await loaded;
  await expect(route).toHaveValue("api");
  expect((await saveProviders(page)).inference_mode).toBe("api");
  await expect(page.getByText("AI route saved.").first()).toBeVisible();
  await route.selectOption("codex");
  await saveProviders(page);
});

async function saveProviders(page: Page) {
  const saved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/settings") && r.request().method() === "PUT",
  );
  await page
    .getByRole("button", { name: "Save AI route", exact: true })
    .click();
  const response = await saved;
  expect(response.ok()).toBeTruthy();
  return response.request().postDataJSON();
}
