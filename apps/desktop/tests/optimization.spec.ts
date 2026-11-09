import { expect, test } from "./fixtures";
import { openMoreSettings, openSettingRow, saveSettingRow } from "./navigation";
import {
  answer,
  saveOutcome,
  hangUp,
  importLead,
  pauseCalling,
  startCalling,
  startDialing,
} from "./simulation";

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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
  if (await pauseCalling(page).isVisible()) await pauseCalling(page).click();
}

test("optimization goals, learning state and provider settings persist", async ({
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
  await openMoreSettings(page, "Learning");
  await expect(
    page.getByRole("region", { name: "Planning assumptions" }),
  ).toBeVisible();
  await page.reload();
  await openMoreSettings(page, "Campaign goal");
  await expect(page.getByLabel("Conversion goal")).toHaveValue("sale_won");
  // The live helper's OpenRouter key has its own Settings row.
  const row = await openSettingRow(page, "openrouter");
  await expect(row.getByLabel("New OpenRouter key")).toBeVisible();
});

test("live events deliver helper tips and conversion recording without call polling", async ({
  page,
}) => {
  await simulation(page);
  // The live helper needs an OpenRouter key; simulation answers with its fixed local model.
  await saveSettingRow(page, "openrouter", "sk-or-test-only");
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  const sockets: string[] = [];
  page.on("websocket", (ws) => sockets.push(ws.url()));
  await page.reload();
  await expect(startCalling(page)).toBeVisible();
  await importLead(page, "Live events fixture", "+14155550706");
  await startDialing(page);
  await answer(page);
  await page
    .getByLabel("Simulation transcript")
    .fill("The budget is not approved.");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  const strip = page.getByTestId("live-coaching-strip");
  await expect(
    strip.getByText("The budget is not approved.", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Private help", exact: true }).click();
  await page
    .getByLabel("Private helper question")
    .fill("How do I answer budget?");
  await page
    .getByRole("button", { name: "Ask privately", exact: true })
    .click();
  await expect(
    strip.getByText("Private answer", { exact: true }),
  ).toBeVisible();
  await expect(
    strip.getByText("How do I answer budget", { exact: false }),
  ).toBeVisible();
  expect(sockets.some((url) => url.endsWith("/v1/live"))).toBeTruthy();
  await pauseCalling(page).click();
  await hangUp(page).click();
  await saveOutcome(page, "qualified_conversation");
  await expect(startCalling(page)).toBeVisible();
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
