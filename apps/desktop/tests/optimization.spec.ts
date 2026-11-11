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
  // D31: StatML is archived; only the number limits remain from its settings.
  await openMoreSettings(page, "Number limits");
  await expect(
    page.getByRole("region", { name: "Number limits" }),
  ).toBeVisible();
  await expect(page.getByLabel("Exploration share")).toHaveCount(0);
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
  const line = "Happy to. What should it cover so it's worth opening?";
  await page
    .getByLabel("Simulation transcript")
    .fill("Could you send me an email?");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  const strip = page.getByTestId("live-coaching-strip");
  const selected = strip.getByTestId("helper-script-part");
  await expect(selected).toContainText(
    "From your script · Objections · Send me an email",
  );
  await expect(selected.locator("p")).toHaveText(line);
  await page.getByRole("button", { name: "Private help", exact: true }).click();
  await page
    .getByLabel("Private helper question")
    .fill("Which line should I use to send an email?");
  await page
    .getByRole("button", { name: "Ask privately", exact: true })
    .click();
  await expect(
    strip.getByText("Private answer", { exact: true }),
  ).toBeVisible();
  await expect(selected.locator("p")).toHaveText(line);
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
