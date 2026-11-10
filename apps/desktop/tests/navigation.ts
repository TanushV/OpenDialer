import { expect, type Locator, type Page } from "@playwright/test";

/**
 * Open a <details> section and confirm it opened. Panels above still grow while background
 * work reports progress (fixed in Phase 5, R5.3), so a click can land after the summary moved.
 */
export async function openDetails(section: Locator) {
  await section.waitFor();
  const isOpen = () => section.evaluate((d) => (d as HTMLDetailsElement).open);
  await expect(async () => {
    if (!(await isOpen()))
      await section.locator(":scope > summary").click({ timeout: 2000 });
    expect(await isOpen()).toBe(true);
  }).toPass({ timeout: 20_000 });
}

/** Settings is one page of rows (R7.2): a row per thing to set, the rest under "Advanced". */
export async function openSettingRow(page: Page, id: string) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const row = page.getByTestId(`setup-${id}`);
  await row.waitFor();
  return row;
}

/** Type a value into a Settings row and save it; the row's real check runs at once. */
export async function saveSettingRow(page: Page, id: string, value: string) {
  const row = await openSettingRow(page, id);
  const add = row.getByRole("button", { name: "Add", exact: true });
  if (await add.isVisible()) await add.click();
  await row.locator("form input").first().fill(value);
  await row.getByRole("button", { name: "Save and check" }).click();
  await expect(row.getByRole("status")).toContainText("Checked");
  return row;
}

/** Opens the do-not-call section under Advanced with optional screening turned on, so its lists are shown. */
export async function openScreening(page: Page) {
  const card = await openMoreSettings(page, "Do-not-call checks and consent");
  const box = card.getByLabel(
    "Check leads against the national do-not-call list before calling",
  );
  if (!(await box.isChecked())) {
    await box.check();
    await card.getByRole("button", { name: "Save", exact: true }).click();
    await expect(box).toBeChecked();
  }
  return card;
}

export async function openMoreSettings(page: Page, section: string) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const more = page.locator("details.more-settings");
  await openDetails(more);
  const item = more
    .locator(".more-list > details")
    .filter({ has: page.locator(":scope > summary", { hasText: section }) });
  await openDetails(item);
  return item;
}

/** The Script page keeps learning, statistics and product knowledge folded below the editor. */
export async function openScriptSection(page: Page, summary: string) {
  await page.getByRole("button", { name: "Script", exact: true }).click();
  const section = page.locator("details.card", {
    has: page.locator(":scope > summary", { hasText: summary }),
  });
  await openDetails(section);
  return section;
}

/**
 * Click until `done` holds. Under load on CI, WebKit can drop a click whose target moved or re-rendered between press
 * and release (Playwright checks stability before the press, not between press and release). Use only for clicks that
 * are safe to repeat: navigation, Try again, Load more.
 */
export async function clickUntil(
  target: Locator,
  done: () => Promise<void>,
  timeout = 30_000,
) {
  await expect(async () => {
    await target.click({ timeout: 5_000 });
    await done();
  }).toPass({ timeout, intervals: [250, 1_000, 2_000] });
}

/** Open a page from the sidebar and wait until it is the one shown. */
export async function goTo(page: Page, title: string) {
  await clickUntil(
    page.getByRole("button", { name: title, exact: true }),
    async () =>
      expect(page.locator(".breadcrumb strong")).toHaveText(title, {
        timeout: 3_000,
      }),
  );
}
