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
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
}

test("the script memory keeps team notes and tests a proposed change against the base", async ({
  page,
}) => {
  await enter(page);
  // The simulated workspace starts from the starter script, the base for proposals.
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
  const form = panel.getByRole("form", { name: "Propose a script change" });
  await form.getByLabel("Script section").selectOption("opening");
  await form
    .getByLabel("New wording")
    .fill("Hi {name}, quick question for you.");
  await form.getByLabel("Script review reference").fill("Reviewed by the lead");
  await form.getByRole("button", { name: "Save proposal" }).click();
  await expect(page.getByText(/must say who is calling/).first()).toBeVisible();
  await form
    .getByLabel("New wording")
    .fill("Hi {name}, {rep} with {seller}. How do you handle this today?");
  await form.getByRole("button", { name: "Save proposal" }).click();
  await expect(
    page.getByText("Proposal saved and its test started"),
  ).toBeVisible();
  await expect(panel.getByText(/^Testing/).first()).toBeVisible();
  await panel.getByLabel("Script decision reason").fill("Not this quarter");
  await panel.getByRole("button", { name: "Reject" }).first().click();
  await expect(
    panel.getByText("Rejected", { exact: true }).first(),
  ).toBeVisible();
});
