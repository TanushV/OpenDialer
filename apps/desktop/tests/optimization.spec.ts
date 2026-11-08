import { expect, test } from "./fixtures";
import {
  openMoreSettings,
  openScriptSection,
  openSettingsCard,
} from "./navigation";
import { importLead, chooseOutcome } from "./simulation";

async function simulation(page: import("@playwright/test").Page) {
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
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
  if (await page.getByRole("button", { name: "Pause dialing" }).isVisible())
    await page.getByRole("button", { name: "Pause dialing" }).click();
}

test("optimization goals, forecast, learning state and provider settings persist", async ({
  page,
}) => {
  await simulation(page);
  await openMoreSettings(page, "Campaign goal");
  await page.getByLabel("Conversion goal").selectOption("sale_won");
  await page.getByLabel("Outcome window (days)").fill("30");
  await page.getByRole("button", { name: "Save campaign goal" }).click();
  await page
    .getByRole("button", { name: "Apply reviewed goal change" })
    .click();
  await expect(page.getByText("Campaign goal saved").first()).toBeVisible();
  await openScriptSection(page, "Call statistics");
  await page
    .getByRole("button", { name: "Refresh learning and capacity" })
    .click();
  await expect(
    page.getByText("Future calling opportunities", { exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("saved-goal")).toContainText("Sale won");
  // Latency breakdowns are diagnostics that no longer sit in the normal view.
  await expect(page.getByText(/p95 inference/)).toHaveCount(0);
  await openMoreSettings(page, "Learning");
  await expect(
    page.getByText("Operational training dataset", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await openMoreSettings(page, "Campaign goal");
  await expect(page.getByLabel("Conversion goal")).toHaveValue("sale_won");
  // ChatGPT and the live help key share the AI card.
  const ai = await openSettingsCard(page, "AI");
  await expect(
    ai.getByLabel("Live help key (OpenRouter, Cerebras)", { exact: true }),
  ).toBeVisible();
});

test("live events deliver cached guidance and conversion recording without call polling", async ({
  page,
}) => {
  await simulation(page);
  const sockets: string[] = [];
  page.on("websocket", (ws) => sockets.push(ws.url()));
  await page.reload();
  await expect(page.getByRole("button", { name: "Go ready" })).toBeVisible();
  await importLead(page, "Live events fixture", "+14155550706");
  await page.getByRole("button", { name: "Go ready" }).click();
  await page.getByRole("button", { name: "Get next lead" }).click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Simulate human answer" }).click();
  await page
    .getByLabel("Simulation transcript")
    .fill("The budget is not approved.");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  await expect(
    page
      .getByTestId("live-coaching-strip")
      .getByText("What outcome would make the investment worthwhile?", {
        exact: false,
      }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Private help", exact: true }).click();
  await expect(
    page.getByText("Seller-approved example · AI unavailable"),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Guidance was useful", exact: true })
    .click();
  await expect(
    page.getByText("Guidance feedback saved", { exact: true }),
  ).toBeVisible();
  expect(sockets.some((url) => url.endsWith("/v1/live"))).toBeTruthy();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "qualified_conversation");
  await page.getByRole("button", { name: "Save outcome" }).click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: /Review call/ })
    .first()
    .click();
  await page.getByLabel("Conversion event").selectOption("meeting_held");
  await page
    .getByLabel("Conversion source reference")
    .fill("Reviewed permissioned simulation meeting");
  await page.getByRole("button", { name: "Record conversion" }).click();
  await expect(
    page.getByText("Conversion recorded", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Conversion correction reason")
    .fill("Meeting entry was a simulation mistake");
  await page
    .getByRole("button", { name: "Retract conversion", exact: true })
    .first()
    .click();
  await expect(
    page.getByText("Conversion retracted", { exact: true }),
  ).toBeVisible();
});
