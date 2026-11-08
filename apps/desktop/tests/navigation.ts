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

/** Settings is one page: a card per thing to set, the rest under "Advanced". */
export async function openSettingsCard(page: Page, title: string) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const card = page.getByTestId(`setting-${title}`);
  await openDetails(card);
  return card;
}

/** Opens the do-not-call card with optional screening turned on, so its lists are shown. */
export async function openScreening(page: Page) {
  const card = await openSettingsCard(page, "Do-not-call check");
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
