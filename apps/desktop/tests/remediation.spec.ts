import { expect, test, type Page, hosted, HOSTED_ONLY } from "./fixtures";
import { openMoreSettings } from "./navigation";
import {
  answer,
  saveOutcome,
  finishCall,
  hangUp,
  importLead,
  isolatedSimulationWorkspace,
  pauseCalling,
  startDialing,
  waitForPreparation,
} from "./simulation";

async function enter(page: Page, connected = false) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (connected) {
    // The previous journey may leave a workspace open; wait for either screen before choosing.
    const signOut = page.getByTitle("Sign out");
    const connect = page.getByRole("button", { name: "Connect to a server" });
    await expect(signOut.or(connect).first()).toBeVisible();
    if (await signOut.isVisible()) await signOut.click();
    await connect.click();
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
    await startDialing(page);
    await expect(page.getByTestId("prospect-brief")).toBeVisible();
    await expect(page.getByTestId("statistical-guidance")).toBeVisible();
    await expect(page.getByTestId("prepared-script")).toBeVisible();
    await answer(page);
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
    await expect(hangUp(page)).toBeInViewport();
    await pauseCalling(page).click();
    await hangUp(page).click();
    await saveOutcome(page, "meeting_booked");
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
  await startDialing(page);
  const script = page.getByTestId("prepared-script");
  await expect(
    script.getByRole("heading", { name: "Gatekeeper", exact: true }),
  ).toBeVisible();
  await expect(
    script.getByRole("heading", { name: "Screening", exact: true }),
  ).toBeVisible();
  await finishCall(page);
});
