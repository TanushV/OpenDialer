import { expect, type Page } from "@playwright/test";
import { goTo } from "./navigation";

/**
 * A configuration journey must not revoke evidence used by unrelated call tests. Every test
 * already has a fresh authority of its own (fixtures.ts), so this only confirms the simulation.
 */
export async function isolatedSimulationWorkspace(page: Page, name: string) {
  void name;
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
  // Setup ends by mounting the Dialer (hidden when another page is open); navigating before then can be undone.
  await expect(page.locator('[aria-label="Call status"]')).toBeAttached();
}

/** Imported leads are researched and scripted before they can be called; wait for that. */
export async function waitForPreparation(page: Page) {
  const panel = page.getByRole("region", { name: "Lead preparation" });
  await expect(panel).toBeVisible();
  await expect(panel.getByText(/in progress|waiting/)).toHaveCount(0, {
    timeout: 30000,
  });
}

/**
 * Add one fictional lead and return to the dialer. Earlier specs on the shared
 * authority may have used every seeded lead's attempts, so a calling journey
 * brings its own.
 */
/** Open Leads and wait until its table has loaded, so nothing below it moves under the next click. */
export async function openLeads(page: Page) {
  await goTo(page, "Leads");
  await expect(page.getByRole("region", { name: "Leads" })).toHaveAttribute(
    "aria-busy",
    "false",
  );
}

export async function importLead(page: Page, name: string, phone: string) {
  await openLeads(page);
  await page
    .getByLabel("CSV lead data")
    .fill(
      `name,phone,company,timezone,region\n${name},${phone},${name} Co,America/Los_Angeles,415`,
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/^1 imported/)).toBeVisible({ timeout: 30_000 });
  await waitForPreparation(page);
  await goTo(page, "Dialer");
}

/** Give a calling journey its own fictional work without resetting shared state. */
export async function prepareSimulationCall(
  page: Page,
  name: string,
  phone: string,
  /** Further fictional leads for a journey that makes more than one call. */
  more: { name: string; phone: string }[] = [],
) {
  await expect(
    page.getByText(/Practice workspace · Calls and provider responses/),
  ).toBeVisible();
  await openLeads(page);
  const rows = [{ name, phone }, ...more]
    .map(
      (lead) =>
        `${lead.name},${lead.phone},${lead.name} Co,America/Los_Angeles,415`,
    )
    .join("\n");
  await page
    .getByLabel("CSV lead data")
    .fill(`name,phone,company,timezone,region\n${rows}`);
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  // Import is a server job; on a loaded machine it can take longer than one expect window.
  await expect(
    page.getByText(new RegExp(`^${1 + more.length} imported`)),
  ).toBeVisible({ timeout: 30_000 });
  await waitForPreparation(page);
  await goTo(page, "Dialer");
  await expect(callStatus(page)).toBeVisible();
}

export const startCalling = (page: Page) =>
  page.getByRole("button", { name: /^Start calling/ });
export const pauseCalling = (page: Page) =>
  page.getByRole("button", { name: /^Pause/ });
export const callStatus = (page: Page) =>
  page.getByRole("status", { name: "Call status" });

/** Start the calling loop; it dials the next lead by itself. */
export async function startDialing(page: Page) {
  await startCalling(page).click();
  await expect(callStatus(page)).toContainText(/Dialing|Ringing|No one left/);
}

/** The six one-key outcomes (section 6); every other outcome is under Add detail. */
const KEYED: Record<string, string> = {
  meeting_booked: "Meeting booked",
  callback_requested: "Callback requested",
  not_interested: "Not interested",
  gatekeeper: "Gatekeeper",
  wrong_party: "Wrong number",
  voicemail: "Voicemail",
};

/** Save an ended call's outcome: one key when it has one, otherwise through Add detail. */
export async function saveOutcome(page: Page, code: string) {
  const keys = page.getByRole("group", { name: "Save outcome" });
  await expect(keys).toBeVisible();
  if (KEYED[code])
    return await keys
      .getByRole("button", { name: new RegExp(`${KEYED[code]}$`) })
      .click();
  await chooseOutcome(page, code);
  await page
    .locator(".wrap-up")
    .last()
    .getByRole("button", { name: "Save outcome", exact: true })
    .click();
}

/** Pick an outcome in the full wrap-up form without saving; opens Add detail on the Dialer first. */
export async function chooseOutcome(page: Page, code: string) {
  const keys = page.getByRole("group", { name: "Save outcome" });
  const wrapUp = page.locator(".wrap-up").last();
  const save = wrapUp.getByRole("button", {
    name: "Save outcome",
    exact: true,
  });
  await expect(keys.or(save)).toBeVisible();
  if (await keys.isVisible())
    await keys.getByRole("button", { name: "Add detail", exact: true }).click();
  await expect(save).toBeVisible();
  const primary = PRIMARY_OUTCOMES[code]
    ? wrapUp.getByRole("button", { name: PRIMARY_OUTCOMES[code], exact: true })
    : null;
  if (primary && (await primary.count())) return await primary.click();
  const select = wrapUp.getByLabel("Disposition");
  if (!(await select.isVisible()))
    await wrapUp.getByText("More outcomes", { exact: true }).click();
  await select.selectOption(code);
}

const PRIMARY_OUTCOMES: Record<string, string> = {
  no_answer: "No answer",
  voicemail: "Voicemail",
  gatekeeper: "Gatekeeper / wrong person",
  not_interested: "Talked: not interested",
  callback_requested: "Talked: callback requested",
  meeting_booked: "Meeting booked",
};

/** Pause, end any live call, and save its owed outcome, so the next journey starts clean. */
export async function finishCall(page: Page) {
  const dialer = page.getByRole("button", { name: "Dialer", exact: true });
  if ((await dialer.isVisible()) && (await dialer.isEnabled()))
    await dialer.click();
  if (!(await callStatus(page).isVisible())) return;
  if (await pauseCalling(page).isVisible()) await pauseCalling(page).click();
  const end = page.getByRole("button", { name: /^(Hang up|Skip now|Skip)\b/ });
  if (await end.first().isVisible()) await end.first().click();
  const keys = page.getByRole("group", { name: "Save outcome" });
  // A skipped call is saved by the loop itself; otherwise save one outcome, as a rep would.
  await expect(keys.or(startCalling(page))).toBeVisible({ timeout: 15_000 });
  if (await keys.isVisible())
    await keys
      .getByRole("button", { name: /Not interested$/ })
      .click({ timeout: 5_000 })
      .catch(() => undefined);
  await expect(startCalling(page)).toBeVisible({ timeout: 15_000 });
}

/** After a test, reopen the workspace and end any call it left behind. */
export async function leaveNoCall(page: Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  )
    return;
  const dialer = page.getByRole("button", { name: "Dialer", exact: true });
  if (await dialer.isEnabled()) await dialer.click();
  await expect(callStatus(page)).toBeVisible();
  // Let the dialer restore a current call from the workspace before checking.
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await finishCall(page);
  await expect(startCalling(page)).toBeVisible();
}

/** Answer the ringing simulated call as a person. */
export async function answer(page: Page) {
  await page
    .getByRole("button", { name: "Simulate human answer", exact: true })
    .click();
  await expect(callStatus(page)).toContainText("Talking");
}

export const hangUp = (page: Page) =>
  page.getByRole("button", { name: /^Hang up/ });
export const outcomeKeys = (page: Page) =>
  page.getByRole("group", { name: "Save outcome" });

/** Open the full wrap-up form under Add detail; returns it. */
export async function addDetail(page: Page) {
  await outcomeKeys(page)
    .getByRole("button", { name: "Add detail", exact: true })
    .click();
  const wrapUp = page.locator(".wrap-up").last();
  await expect(wrapUp).toBeVisible();
  return wrapUp;
}
