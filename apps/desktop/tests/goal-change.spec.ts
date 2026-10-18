import { expect, test } from "@playwright/test";

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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  const before = await page
    .getByRole("combobox", { name: "Conversion goal", exact: true })
    .inputValue();
  const after = before === "sale_won" ? "meeting_held" : "sale_won";
  await page
    .getByRole("combobox", { name: "Conversion goal", exact: true })
    .selectOption(after);
  await page
    .getByRole("button", { name: "Save optimization settings" })
    .click();
  await expect(
    page.getByRole("region", { name: "Review goal change" }),
  ).toBeVisible();
  await expect(
    page.getByText(/Previously completed prospects.*review/i),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "Conversion goal", exact: true }),
  ).toHaveValue(before);
  await page
    .getByRole("combobox", { name: "Conversion goal", exact: true })
    .selectOption(after);
  await page
    .getByRole("button", { name: "Save optimization settings" })
    .click();
  await page
    .getByRole("button", { name: "Apply reviewed goal change" })
    .click();
  await expect(
    page.getByText("Optimization settings saved", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  if (
    (await page.getByLabel("Conversion goal").inputValue()) !==
    "qualified_conversation"
  ) {
    await page
      .getByLabel("Conversion goal")
      .selectOption("qualified_conversation");
    await page
      .getByRole("button", { name: "Save optimization settings" })
      .click();
    await page
      .getByRole("button", { name: "Apply reviewed goal change" })
      .click();
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
  await page.getByLabel("Disposition").selectOption("qualified_conversation");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByLabel("Conversion goal").selectOption("sale_won");
  await page
    .getByRole("button", { name: "Save optimization settings" })
    .click();
  await page
    .getByRole("button", { name: "Apply reviewed goal change" })
    .click();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByRole("button", { name: `Open lead ${name}`, exact: true })
    .click();
  await page
    .getByText("Prior conversation & next step", { exact: true })
    .click();
  await expect(
    page.getByText("Outreach paused: Goal changed", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Goal review reason", { exact: true })
    .fill("Reviewed relationship; resume when eligible");
  await page
    .getByRole("button", { name: "Resume outreach after review", exact: true })
    .click();
  await expect(
    page.getByText("Outreach paused: Goal changed", { exact: true }),
  ).toBeHidden();
  await page.reload();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByRole("button", { name: `Open lead ${name}`, exact: true })
    .click();
  await page
    .getByText("Prior conversation & next step", { exact: true })
    .click();
  await expect(
    page.getByText("Outreach paused: Goal changed", { exact: true }),
  ).toBeHidden();
});
