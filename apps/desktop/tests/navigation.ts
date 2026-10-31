import type { Page } from "@playwright/test";

/** Settings is one page: a card per thing to set, the rest under "More settings". */
export async function openSettingsCard(page: Page, title: string) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const card = page.getByTestId(`setting-${title}`);
  await card.waitFor();
  if (!(await card.evaluate((d) => (d as HTMLDetailsElement).open)))
    await card.locator(":scope > summary").click();
  return card;
}

export async function openMoreSettings(page: Page, section: string) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const more = page.locator("details.more-settings");
  await more.waitFor();
  if (!(await more.evaluate((d) => (d as HTMLDetailsElement).open)))
    await more.locator(":scope > summary").click();
  const item = more
    .locator(".more-list > details")
    .filter({ has: page.locator(":scope > summary", { hasText: section }) });
  await item.waitFor();
  if (!(await item.evaluate((d) => (d as HTMLDetailsElement).open)))
    await item.locator(":scope > summary").click();
  return item;
}

/** The Script page keeps learning, statistics and product knowledge folded below the editor. */
export async function openScriptSection(page: Page, summary: string) {
  await page.getByRole("button", { name: "Script", exact: true }).click();
  const section = page.locator("details.card", {
    has: page.locator(":scope > summary", { hasText: summary }),
  });
  await section.waitFor();
  if (!(await section.evaluate((d) => (d as HTMLDetailsElement).open)))
    await section.locator(":scope > summary").click();
  return section;
}
