import { expect, type Page } from "@playwright/test";
import { clickUntil, goTo } from "./navigation";

/** Deliver a callback through the live workspace stream after the app is on the page under test. */
export async function prepareIncomingCall(
  page: Page,
  call: {
    id: string;
    state: "ringing" | "connected";
    prospect?: { name: string; company: string };
  },
) {
  const session = page.waitForResponse(
    (response) =>
      response.ok() &&
      response.request().method() === "POST" &&
      /\/v1\/(setup|local\/session)$/.test(response.url()),
  );
  let arrived = false;
  let deliver: ((tenantId: string) => void) | undefined;
  await page.routeWebSocket("**/v1/live", (socket) => {
    socket.onMessage((message) => {
      const { after } = JSON.parse(String(message));
      deliver = (tenantId) => {
        const sequence = after + 1;
        socket.send(
          JSON.stringify({
            cursor: sequence,
            events: [
              {
                event_id: `arrival-${call.id}`,
                sequence,
                tenant_id: tenantId,
                type: "task.updated",
                entity_id: call.id,
                entity_version: 1,
                payload: {},
              },
            ],
          }),
        );
      };
    });
  });
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    if (arrived)
      data.tasks.push({
        id: call.id,
        version: 1,
        inbound: true,
        inbound_state: call.state,
        routed_rep_id: data.user.id,
        callback_phone: "+14155550111",
        lead_name: call.prospect?.name,
        lead_company: call.prospect?.company,
        lead_id: null,
        due_at: null,
        status: "open",
        action: "Return call",
        route: "rep",
        routing_attempts:
          call.state === "ringing"
            ? [
                {
                  id: `leg-${call.id}`,
                  rep_id: data.user.id,
                  state: "ringing",
                  provider_id: `provider-${call.id}`,
                },
              ]
            : [],
      });
    await route.fulfill({ response, json: data });
  });
  return async () => {
    const { tenant_id } = await (await session).json();
    await expect.poll(() => Boolean(deliver)).toBe(true);
    arrived = true;
    deliver?.(tenant_id);
  };
}

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
  const end = page.getByRole("button", { name: /^(Hang up|Skip now|Skip)\b/ });
  const keys = page.getByRole("group", { name: "Save outcome" });
  const tryClick = (target: ReturnType<Page["getByRole"]>) =>
    target.click({ timeout: 2_000 }).catch(() => undefined);
  // Repeat until calling is paused with nothing owed: a click WebKit drops under load, or a call that starts
  // ringing just as Pause lands, only costs another round. A skipped call is saved by the loop itself; an
  // answered one gets one outcome, as a rep would save it.
  await expect(async () => {
    if (await pauseCalling(page).isVisible())
      await tryClick(pauseCalling(page));
    if (await end.first().isVisible()) await tryClick(end.first());
    if (await keys.isVisible())
      await tryClick(keys.getByRole("button", { name: /Not interested$/ }));
    await expect(startCalling(page)).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 60_000 });
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
  // Safe to repeat: the button leaves once the call is answered. WebKit can drop the click while the view re-renders.
  await clickUntil(
    page.getByRole("button", { name: "Simulate human answer", exact: true }),
    () => expect(callStatus(page)).toContainText("Talking", { timeout: 3_000 }),
  );
}

export const hangUp = (page: Page) =>
  page.getByRole("button", { name: /^Hang up/ });
export const outcomeKeys = (page: Page) =>
  page.getByRole("group", { name: "Save outcome" });

/** Open the full wrap-up form under Add detail; returns it. */
export async function addDetail(page: Page) {
  const wrapUp = page.locator(".wrap-up").last();
  // Opening the detail is safe to repeat; WebKit can drop the click while the call view re-renders.
  await clickUntil(
    outcomeKeys(page).getByRole("button", { name: "Add detail", exact: true }),
    () => expect(wrapUp).toBeVisible({ timeout: 3_000 }),
  );
  return wrapUp;
}
