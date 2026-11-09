import { expect, test } from "./fixtures";
import { finishCall, startDialing, waitForPreparation } from "./simulation";

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
  await expect(page.getByText(/^1 imported/)).toBeVisible({ timeout: 30_000 });
  await waitForPreparation(page);
}

test("manage lists and archive/restore selected leads without losing records", async ({
  page,
}) => {
  await enter(page);
  await add(page, "Managed list 091", "+14155550961");
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  const list = page
    .getByRole("region", { name: "Lead lists" })
    .getByRole("row")
    .filter({ has: page.getByText("Managed list 091", { exact: true }) });
  await expect(list).toBeVisible();
  await list.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(list.getByText("Paused", { exact: true })).toBeVisible();
  await list.getByRole("button", { name: "Resume", exact: true }).click();
  await list.getByRole("button", { name: "Remove list", exact: true }).click();
  await expect(list).toBeHidden();
  await page.getByLabel("Show removed lists", { exact: true }).check();
  await expect(list.getByText("Removed", { exact: true })).toBeVisible();
  await list.getByRole("button", { name: "Restore list", exact: true }).click();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("Filter leads by list")
    .selectOption("Managed list 091");
  await page.getByLabel("Select Managed list 091").check();
  await page.getByLabel("Lead action", { exact: true }).selectOption("archive");
  await page
    .getByRole("button", { name: "Apply to selected leads", exact: true })
    .click();
  const contact = page
    .getByRole("region", { name: "Leads" })
    .getByRole("row")
    .filter({
      has: page.getByText("Managed list 091", { exact: true }),
    });
  await expect(contact).toBeHidden();
  await page.getByLabel("Lead view", { exact: true }).selectOption("removed");
  await expect(contact.getByText("Removed", { exact: true })).toBeVisible();
  await page.getByLabel("Select Managed list 091").check();
  await page.getByLabel("Lead action", { exact: true }).selectOption("restore");
  await page
    .getByRole("button", { name: "Apply to selected leads", exact: true })
    .click();
  await expect(contact).toBeHidden();
  await page.getByLabel("Lead view", { exact: true }).selectOption("active");
  await expect(contact).toBeVisible();
  await expect(contact.getByText("Removed", { exact: true })).toBeHidden();
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
  await startDialing(page);
  await expect(page.getByTestId("lead-name")).toHaveText("Dial only 091");
  await finishCall(page);
  await page.getByLabel("Dial from all active lists", { exact: true }).check();
});

test("a dialing-list choice survives reload while the previous queue release is pending", async ({
  page,
}) => {
  await enter(page);
  await add(page, "Reload list 100", "+14155550793");
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await page
    .getByLabel("Dial from all active lists", { exact: true })
    .uncheck();
  const choice = page.getByLabel("Dial list Reload list 100", { exact: true });
  await expect(choice).toBeEnabled();
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/queue/release", async (route) => {
    await delayed;
    await route.fulfill({ json: { released: true } });
  });
  try {
    await choice.check();
    await page.reload();
    await expect(choice).toBeChecked();
  } finally {
    release();
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
  await page.getByLabel("Dial from all active lists", { exact: true }).check();
});

test("a rejected dialing-list change restores the saved choice", async ({
  page,
}) => {
  await enter(page);
  await add(page, "Rejected list 100", "+14155550794");
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await page
    .getByLabel("Dial from all active lists", { exact: true })
    .uncheck();
  const choice = page.getByLabel("Dial list Rejected list 100", {
    exact: true,
  });
  await expect(choice).toBeEnabled();
  let reject!: () => void;
  const delayed = new Promise<void>((resolve) => {
    reject = resolve;
  });
  await page.route("**/v1/queue/release", async (route) => {
    await delayed;
    await route.fulfill({
      status: 409,
      json: { detail: "Call is still active." },
    });
  });
  try {
    await choice.check();
    reject();
    await expect(choice).not.toBeChecked();
    await page.reload();
    await expect(choice).not.toBeChecked();
  } finally {
    reject();
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
  await page.getByLabel("Dial from all active lists", { exact: true }).check();
});
