import { expect, test } from "./fixtures";
import { chooseOutcome } from "./simulation";

/** The campaign goal is a setup step; it opens collapsed. */
async function openGoal(page: import("@playwright/test").Page) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByTestId("setup-campaign").locator(":scope > summary").click();
}

test("campaign goal changes are previewed before application and survive reload", async ({
  page,
}) => {
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
  await openGoal(page);
  const before = await page
    .getByRole("combobox", { name: "Conversion goal", exact: true })
    .inputValue();
  const after = before === "sale_won" ? "meeting_held" : "sale_won";
  await page
    .getByRole("combobox", { name: "Conversion goal", exact: true })
    .selectOption(after);
  await page.getByRole("button", { name: "Save campaign goal" }).click();
  await expect(
    page.getByRole("region", { name: "Review goal change" }),
  ).toBeVisible();
  await expect(
    page.getByText(/Previously completed prospects.*review/i),
  ).toBeVisible();
  await page.reload();
  await openGoal(page);
  await expect(
    page.getByRole("combobox", { name: "Conversion goal", exact: true }),
  ).toHaveValue(before);
  await page
    .getByRole("combobox", { name: "Conversion goal", exact: true })
    .selectOption(after);
  await page.getByRole("button", { name: "Save campaign goal" }).click();
  await page
    .getByRole("button", { name: "Apply reviewed goal change" })
    .click();
  await expect(
    page.getByText("Campaign goal saved", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await openGoal(page);
  await expect(
    page.getByRole("combobox", { name: "Conversion goal", exact: true }),
  ).toHaveValue(after);
});

test("rep explicitly reviews a reopened prospect before resuming outreach", async ({
  page,
}) => {
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
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("CSV lead data")
    .fill(
      "name,phone,company,timezone,region\nGoal Fixture,+14155550181,Example,America/Los_Angeles,415",
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await openGoal(page);
  if (
    (await page.getByLabel("Conversion goal").inputValue()) !==
    "qualified_conversation"
  ) {
    await page
      .getByLabel("Conversion goal")
      .selectOption("qualified_conversation");
    await page.getByRole("button", { name: "Save campaign goal" }).click();
    await page
      .getByRole("button", { name: "Apply reviewed goal change" })
      .click();
    await expect(page.getByText("Campaign goal saved").first()).toBeVisible();
  }
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  if (
    await page
      .getByRole("button", { name: "Go ready", exact: true })
      .isVisible()
  )
    await page.getByRole("button", { name: "Go ready", exact: true }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  const name = await page.getByTestId("lead-name").textContent();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "qualified_conversation");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  await openGoal(page);
  await page.getByLabel("Conversion goal").selectOption("sale_won");
  await page.getByRole("button", { name: "Save campaign goal" }).click();
  await page
    .getByRole("button", { name: "Apply reviewed goal change" })
    .click();
  await expect(page.getByText("Campaign goal saved").first()).toBeVisible();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByRole("button", { name: `Open lead ${name}`, exact: true })
    .click();
  // Scope to the packet dialog: the paused dialer keeps its last brief mounted.
  await page
    .getByRole("dialog", { name: "Lead packet" })
    .getByText("Prior conversation & next step", { exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog", { name: "Lead packet" })
      .getByText("Outreach paused: Goal changed", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("dialog", { name: "Lead packet" })
    .getByLabel("Goal review reason", { exact: true })
    .fill("Reviewed relationship; resume when eligible");
  await page
    .getByRole("dialog", { name: "Lead packet" })
    .getByRole("button", { name: "Resume outreach after review", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog", { name: "Lead packet" })
      .getByText("Outreach paused: Goal changed", { exact: true }),
  ).toBeHidden();
  await page.reload();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByRole("button", { name: `Open lead ${name}`, exact: true })
    .click();
  // Scope to the packet dialog: the paused dialer keeps its last brief mounted.
  await page
    .getByRole("dialog", { name: "Lead packet" })
    .getByText("Prior conversation & next step", { exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog", { name: "Lead packet" })
      .getByText("Outreach paused: Goal changed", { exact: true }),
  ).toBeHidden();
});
