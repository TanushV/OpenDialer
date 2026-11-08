import { expect, test } from "./fixtures";
import { openScriptSection } from "./navigation";

async function enter(page: import("@playwright/test").Page) {
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
}

test("the script memory keeps team notes", async ({ page }) => {
  await enter(page);
  // The simulated workspace starts from the starter script.
  await openScriptSection(page, "What's working");
  const panel = page.getByRole("region", { name: "Script memory" });
  await expect(panel.getByText(/^Base script v\d+/)).toBeVisible();
  await panel.getByLabel("Team notes").fill("Lead with the time saved.");
  await panel.getByRole("button", { name: "Save team notes" }).click();
  await expect(
    panel
      .getByLabel("Script memory document")
      .getByText("Lead with the time saved."),
  ).toBeVisible();
});
