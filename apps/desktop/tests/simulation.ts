import { expect, type Page } from "@playwright/test";

/** A configuration journey must not revoke evidence used by unrelated call tests. */
export async function isolatedSimulationWorkspace(page: Page, name: string) {
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
  const url = "http://127.0.0.1:8766";
  const login = await page.request.post(`${url}/v1/auth/session`, {
    data: {
      email: "team@example.test",
      password: "Connected-test-password-123",
    },
  });
  expect(login.ok()).toBeTruthy();
  const administrator = await login.json();
  const created = await page.request.post(`${url}/v1/admin/workspaces`, {
    headers: { authorization: `Bearer ${administrator.access_token}` },
    data: {
      seller_name: name,
      campaign_name: name,
      email: `${name}@example.test`,
      password: "Disposable-workspace-password-123",
    },
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const seeded = await page.request.post(`${url}/v1/simulation/seed`, {
    headers: { authorization: `Bearer ${session.access_token}` },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.getByTitle("Sign out").click();
  await page.getByRole("button", { name: "Connect to a server" }).click();
  await page.getByLabel("Server URL").fill(url);
  await page.getByLabel("Email").fill(`${name}@example.test`);
  await page
    .getByLabel("Password", { exact: true })
    .fill("Disposable-workspace-password-123");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
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
export async function importLead(page: Page, name: string, phone: string) {
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("CSV lead data")
    .fill(
      `name,phone,company,timezone,region\n${name},${phone},${name} Co,America/Los_Angeles,415`,
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/^1 imported/)).toBeVisible();
  await waitForPreparation(page);
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
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
  await page.getByRole("button", { name: "Leads", exact: true }).click();
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
  await expect(
    page.getByText(new RegExp(`^${1 + more.length} imported`)),
  ).toBeVisible();
  await waitForPreparation(page);
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  const ready = page.getByRole("button", { name: "Go ready", exact: true });
  await expect(
    ready.or(page.getByRole("button", { name: "Pause dialing", exact: true })),
  ).toBeVisible();
  if (await ready.isVisible()) await ready.click();
}

const PRIMARY_OUTCOMES: Record<string, string> = {
  no_answer: "No answer",
  voicemail: "Voicemail",
  gatekeeper: "Gatekeeper / wrong person",
  not_interested: "Talked: not interested",
  callback_requested: "Talked: callback requested",
  meeting_booked: "Meeting booked",
};

/** Choose a wrap-up outcome: one click when common, otherwise "More outcomes". */
export async function chooseOutcome(page: Page, code: string) {
  const wrapUp = page.locator(".wrap-up").last();
  await expect(
    wrapUp.getByRole("button", { name: "Save outcome", exact: true }),
  ).toBeVisible();
  const primary = PRIMARY_OUTCOMES[code]
    ? wrapUp.getByRole("button", { name: PRIMARY_OUTCOMES[code], exact: true })
    : null;
  if (primary && (await primary.count())) return await primary.click();
  const select = wrapUp.getByLabel("Disposition");
  if (!(await select.isVisible()))
    await wrapUp.getByText("More outcomes", { exact: true }).click();
  await select.selectOption(code);
}

/** End any live call and save its owed wrap-up so the next journey starts clean. */
export async function finishCall(page: Page) {
  const hangup = page.getByRole("button", { name: "Hang up", exact: true });
  const save = page.getByRole("button", { name: "Save outcome", exact: true });
  if (await hangup.isVisible()) {
    await hangup.click();
    await expect(save).toBeVisible();
  }
  if (await save.isVisible()) {
    await chooseOutcome(page, "no_answer");
    // Cleanup saves again, as a rep would, when the shared PostgreSQL
    // authority answers a conflict with a concurrent post-hangup write.
    for (let attempt = 1; ; attempt++) {
      await save.click();
      try {
        await expect(save).toBeHidden();
        return;
      } catch (error) {
        if (attempt >= 3) throw error;
      }
    }
  }
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
  const pause = page.getByRole("button", {
    name: "Pause dialing",
    exact: true,
  });
  const ready = page.getByRole("button", { name: "Go ready", exact: true });
  await expect(ready.or(pause)).toBeVisible();
  // Let the dialer restore a current call from the workspace before checking.
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await finishCall(page);
  if (await pause.isVisible()) await pause.click();
  await expect(ready).toBeVisible();
}
