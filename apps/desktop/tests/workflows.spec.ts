import { expect, test } from "./fixtures";
import { openMoreSettings, openSettingsCard } from "./navigation";
import { chooseOutcome, waitForPreparation } from "./simulation";

async function enterSimulation(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  ) {
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  }
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
  if (await page.getByRole("button", { name: "Pause dialing" }).isVisible())
    await page.getByRole("button", { name: "Pause dialing" }).click();
}

test("local setup, readiness, calling, evidence, human notes and opt-out", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1028, height: 749 });
  await enterSimulation(page);
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("CSV lead data")
    .fill(
      "name,phone,company,timezone,region\nWorkflow Call,+12125550931,Workflow Example,America/New_York,212",
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/^1 imported/)).toBeVisible();
  await waitForPreparation(page);
  await page.getByLabel("Search leads").fill("Workflow Call");
  await page
    .getByRole("button", { name: "Search lead history", exact: true })
    .click();
  await expect(page.getByText("Workflow Call", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Your next conversation" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Go ready" }).click();
  await page.getByRole("button", { name: "Get next lead" }).click();
  await expect(page.getByTestId("lead-name")).not.toBeEmpty();
  // Reopen this journey's own call: other specs add calls to the shared list.
  const reviewCall = page
    .getByRole("button", {
      name: `Review call ${await page.getByTestId("lead-name").innerText()}`,
      exact: true,
    })
    .first();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(page.getByText("RINGING", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Simulate human answer" }).click();
  await expect(page.getByText("HUMAN", { exact: true })).toBeVisible();
  await page
    .getByLabel("Simulation transcript")
    .fill("We already use another provider.");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  // The ephemeral transcript is an optional expansion during the call.
  await page.getByText("Live transcript", { exact: false }).click();
  await expect(
    page.getByText("We already use another provider.", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "callback_requested");
  await page.getByRole("button", { name: "Save outcome" }).click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await reviewCall.click();
  await page
    .getByLabel("Your notes")
    .fill("Keep this human correction after final processing.");
  await page.getByRole("button", { name: "Save notes" }).click();
  await expect(page.getByText("Notes saved")).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await reviewCall.click();
  await expect(page.getByLabel("Your notes")).toHaveValue(
    "Keep this human correction after final processing.",
  );
});

test("admin inventory, lead import, policy, preparation and real readiness boundary", async ({
  page,
}) => {
  await enterSimulation(page);
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("CSV lead data")
    .fill(
      "name,phone,company,timezone\nTaylor Example,+12125550177,Example Co,America/New_York",
    );
  await page.getByLabel("List name").fill("Workflow journey list");
  await page.getByRole("button", { name: "Import leads" }).click();
  await expect(page.getByText(/^1 imported/)).toBeVisible();
  await waitForPreparation(page);
  // The app asks for research and a script for each imported lead before calling.
  await expect(
    page
      .getByRole("region", { name: "Lead preparation" })
      .getByRole("listitem")
      .filter({ hasText: "Workflow journey list" })
      .getByText("1 of 1 ready to call"),
  ).toBeVisible();
  await page.getByLabel("Search leads").fill("Taylor Example");
  await page
    .getByRole("button", { name: "Search lead history", exact: true })
    .click();
  await expect(page.getByText("Taylor Example", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Numbers & callbacks" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Plan state coverage" }).click();
  // One planning request; the saved plan is then read back with backoff.
  await expect(page.getByText("State coverage proposal")).toBeVisible({
    timeout: 20000,
  });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Check readiness" }).click();
  await expect(page.getByRole("region", { name: "Readiness" })).toBeVisible();
});

test("connected authentication and shared state survive reconnect", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (await page.getByTitle("Sign out").isVisible())
    await page.getByTitle("Sign out").click();
  await page.getByRole("button", { name: "Connect to a server" }).click();
  await page.getByLabel("Server URL").fill("http://127.0.0.1:8766");
  await page.getByLabel("Email").fill("team@example.test");
  await page
    .getByLabel("Password", { exact: true })
    .fill("Connected-test-password-123");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await expect(page.getByText("Jordan Ellis", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
});

test("advanced administration exposes evidence-controlled workflows", async ({
  page,
}) => {
  await enterSimulation(page);
  // Trackers are advanced analysis configuration: Settings → More settings.
  await openMoreSettings(page, "Learning experiments and trackers");
  await page.getByLabel("Tracker name").fill("Existing supplier");
  await page
    .getByLabel("Tracker definition")
    .fill("A prospect explicitly mentions an existing supplier.");
  await page
    .getByRole("textbox", { name: "Keyword phrases", exact: true })
    .fill("another provider\nexisting supplier");
  await page.getByRole("button", { name: "Publish tracker" }).click();
  await expect(
    page.getByText("Existing supplier", { exact: true }),
  ).toBeVisible();
  // Business confirmation is one button in Calling hours.
  const hours = await openSettingsCard(page, "Calling hours");
  await expect(hours).toContainText(/Business confirmed|Confirm our business/);
});

test("script wording is tested from the script memory and purchases are never offered", async ({
  page,
}) => {
  await enterSimulation(page);
  // The experiment view no longer publishes opener variants; script memory tests them.
  await openMoreSettings(page, "Learning experiments and trackers");
  await expect(
    page
      .getByLabel("Experiment type")
      .locator("option", { hasText: /variant/i }),
  ).toHaveCount(0);
  await expect(
    page.getByText(/Script wording is tested from Script memory/),
  ).toBeVisible();
  // Purchases are disabled by policy, so no quote or rental tool is shown.
  await page.getByRole("button", { name: "Numbers", exact: true }).click();
  await expect(page.getByLabel("Procurement area code")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Get carrier quotes" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: /purchase|buy/i })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  // Device selection is the pre-ready "Check audio" step.
  await page.getByText("Check audio", { exact: true }).click();
  await expect(page.getByLabel("Microphone")).toBeVisible();
});
