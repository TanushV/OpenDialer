import { expect, test } from "./fixtures";
import { waitForPreparation, finishCall } from "./simulation";

async function enter(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
}

async function add(
  page: import("@playwright/test").Page,
  list: string,
  phone: string,
) {
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByLabel("List name", { exact: true }).fill(list);
  await page
    .getByLabel("CSV lead data")
    .fill(`name,phone,timezone\n${list},${phone},America/Los_Angeles`);
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/^1 imported/)).toBeVisible();
  await waitForPreparation(page);
}

test("manage lists and archive/restore selected leads without losing records", async ({
  page,
}) => {
  await enter(page);
  await add(page, "Managed list 091", "+14155550891");
  await page.getByRole("button", { name: "Lists", exact: true }).click();
  const list = page
    .getByRole("row")
    .filter({ has: page.getByText("Managed list 091", { exact: true }) });
  await expect(list).toBeVisible();
  await list.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(list.getByText("Paused", { exact: true })).toBeVisible();
  await list.getByRole("button", { name: "Resume", exact: true }).click();
  await list.getByRole("button", { name: "Archive", exact: true }).click();
  await expect(list.getByText("Archived", { exact: true })).toBeVisible();
  await list.getByRole("button", { name: "Restore", exact: true }).click();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("Filter leads by list")
    .selectOption("Managed list 091");
  await page.getByLabel("Select Managed list 091").check();
  await page.getByLabel("Lead action", { exact: true }).selectOption("archive");
  await page
    .getByRole("button", { name: "Apply to selected leads", exact: true })
    .click();
  await expect(
    page
      .getByRole("row")
      .filter({ has: page.getByText("Managed list 091", { exact: true }) })
      .getByText("Archived", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Select Managed list 091").check();
  await page.getByLabel("Lead action", { exact: true }).selectOption("restore");
  await page
    .getByRole("button", { name: "Apply to selected leads", exact: true })
    .click();
  await expect(
    page
      .getByRole("row")
      .filter({ has: page.getByText("Managed list 091", { exact: true }) })
      .getByText("Archived", { exact: true }),
  ).toBeHidden();
});

test("Dial from selected lists reserves only that pool and keeps scope across reload", async ({
  page,
}) => {
  await enter(page);
  await add(page, "Dial only 091", "+14155550892");
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await page
    .getByLabel("Dial from all active lists", { exact: true })
    .uncheck();
  await page.getByLabel("Dial list Dial only 091", { exact: true }).check();
  await page.reload();
  await expect(
    page.getByLabel("Dial list Dial only 091", { exact: true }),
  ).toBeChecked();
  const ready = page.getByRole("button", { name: "Go ready", exact: true });
  if (await ready.isVisible()) await ready.click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await expect(page.getByTestId("lead-name")).toHaveText("Dial only 091");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Hang up", exact: true }),
  ).toBeVisible();
  await finishCall(page);
  await page.getByLabel("Dial from all active lists", { exact: true }).check();
});
