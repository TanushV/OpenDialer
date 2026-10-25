import { expect, test } from "./fixtures";

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
  // A base script template is the starting point for proposals.
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await page
    .getByLabel("Script topic", { exact: true })
    .fill("invoice follow-up");
  await page
    .getByRole("button", { name: "Save script and preparation" })
    .click();
  await expect(page.getByText("Script and preparation saved")).toBeVisible();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByText("Script memory & variations", { exact: true }).click();
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
  await form.getByLabel("Script section").selectOption("opener");
  await form
    .getByLabel("New wording")
    .fill("Hi {name}, quick question about {topic}.");
  await form.getByLabel("Script review reference").fill("Reviewed by the lead");
  await form.getByRole("button", { name: "Save proposal" }).click();
  await expect(page.getByText(/keeps who is calling/).first()).toBeVisible();
  await form
    .getByLabel("New wording")
    .fill("Hi {name}, {rep} with {seller}. How do you handle {topic} today?");
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
