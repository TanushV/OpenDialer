import { expect, test, type Page, AUTHORITY, setUpAuthority } from "./fixtures";
import {
  addDetail,
  answer,
  callStatus,
  saveOutcome,
  finishCall,
  hangUp,
  outcomeKeys,
  pauseCalling,
  prepareSimulationCall,
  startCalling,
} from "./simulation";
import { zonedInstant } from "../src/state/zones";
import { clickUntil } from "./navigation";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await finishCall(page);
});

async function enter(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const choice = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await choice.isVisible()) await choice.click();
  // Setup finishes with its own workspace load; tests that intercept loads start after it.
  await expect(startCalling(page).or(pauseCalling(page))).toBeVisible();
}

/** One owned fictional lead in a live simulated call; returns its packet and call. */
async function startCall(page: Page, name: string, phone: string) {
  await enter(page);
  await prepareSimulationCall(page, name, phone);
  const packet = page.waitForResponse((r) =>
    /\/v1\/leads\/[^/]+\/packet$/.test(r.url()),
  );
  const started = page.waitForResponse((r) =>
    r.url().endsWith("/v1/calls/start"),
  );
  await startCalling(page).click();
  return {
    packet: await (await packet).json(),
    call: await (await started).json(),
  };
}

/** Session tests check renewal only; an empty queue keeps them from placing a call. */
async function emptyQueue(page: Page) {
  await page.route("**/v1/queue/next", (route) =>
    route.fulfill({
      status: 409,
      json: {
        detail: {
          code: "NO_ELIGIBLE_LEADS",
          reasons: [],
          message: "No one to call.",
        },
      },
    }),
  );
}

function countStarts(page: Page) {
  const seen = { starts: 0 };
  page.on("request", (request) => {
    if (request.url().endsWith("/v1/calls/start")) seen.starts++;
  });
  return seen;
}

test("at 1024x720 the helper, script opener, hang-up and opt-out stay visible and focus survives a live update", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 720 });
  const { call } = await startCall(page, "Small window rep", "+14155550601");
  await answer(page);
  const visible = [
    page.getByTestId("live-coaching-strip"),
    page
      .getByTestId("prepared-script")
      .locator(".script-reader > section p")
      .first(),
    hangUp(page),
    page.getByRole("button", { name: /opt.out/i }),
  ];
  for (const item of visible) await expect(item).toBeInViewport();
  await page.getByRole("button", { name: "Private help", exact: true }).click();
  const question = page.getByLabel("Private helper question");
  await question.fill("What did they already answer?");
  await expect(question).toBeFocused();
  // A prospect turn arrives over the live channel while the rep is typing.
  const updated = page.waitForResponse(
    (r) => r.url().endsWith(`/v1/calls/${call.id}`) && r.ok(),
  );
  const local = await page.request.post(`${AUTHORITY}/v1/local/session`, {
    headers: { "X-Local-Token": "e2e-local-token" },
    data: {},
  });
  const token = (await local.json()).access_token;
  const turn = await page.request.post(
    `${AUTHORITY}/v1/calls/${call.id}/transcript`,
    {
      headers: { authorization: `Bearer ${token}` },
      data: {
        segment_id: crypto.randomUUID(),
        revision: 1,
        side: "remote",
        text: "We already answered the budget question.",
        is_final: true,
      },
    },
  );
  expect(turn.ok()).toBeTruthy();
  await updated;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await expect(question).toBeFocused();
  await expect(question).toHaveValue("What did they already answer?");
  for (const item of visible) await expect(item).toBeInViewport();
});

test("the brief says why this lead is next and shows no learned estimates", async ({
  page,
}) => {
  // D31: StatML is archived; the queue is callbacks due, never called, then longest waiting.
  await startCall(page, "Order fixture", "+14155550611");
  const context = page.getByTestId("calling-context");
  await expect(context).toContainText("Why this lead: Never called");
  await expect(context).not.toContainText("Pickup");
  await expect(context).not.toContainText("Estimates");
});

test("review notes start empty and the AI summary stays read-only", async ({
  page,
}) => {
  await page.route("**/v1/calls/*/review", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.human_notes = null;
    body.summary = {
      status: "ready",
      summary: "AI drafted summary of the call.",
    };
    await route.fulfill({ response, json: body });
  });
  const { packet } = await startCall(
    page,
    "Review notes fixture",
    "+14155550602",
  );
  await answer(page);
  await clickUntil(pauseCalling(page), () =>
    expect(page.getByText("Calling stops after this call")).toBeVisible({
      timeout: 3_000,
    }),
  );
  await hangUp(page).click();
  await saveOutcome(page, "not_interested");
  await expect(startCalling(page)).toBeVisible();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: `Review call ${packet.lead.name}` })
    .first()
    .click();
  await expect(page.getByLabel("Your notes", { exact: true })).toHaveValue("");
  await expect(page.getByRole("region", { name: "AI summary" })).toContainText(
    "AI drafted summary of the call.",
  );
});

test("a saved outcome stays saved when loading the next lead fails", async ({
  page,
}) => {
  const { call } = await startCall(
    page,
    "Refresh failure fixture",
    "+14155550604",
  );
  await answer(page);
  await hangUp(page).click();
  await page.route("**/v1/leads/*/packet", (route) =>
    route.fulfill({ status: 500, json: { detail: "Synthetic packet outage" } }),
  );
  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/disposition") && r.ok(),
  );
  await saveOutcome(page, "not_interested");
  await saved;
  // The next call cannot load: calling pauses with the reason and the outcome stays saved.
  await expect(page.getByText(/Synthetic packet outage/)).toBeVisible();
  await expect(startCalling(page)).toBeVisible();
  const local = await page.request.post(`${AUTHORITY}/v1/local/session`, {
    headers: { "X-Local-Token": "e2e-local-token" },
    data: {},
  });
  const token = (await local.json()).access_token;
  const stored = await page.request.get(`${AUTHORITY}/v1/calls/${call.id}`, {
    headers: { authorization: `Bearer ${token}` },
  });
  expect((await stored.json()).outcome).toBe("not_interested");
});

test("an unsaved outcome holds the next call and keeps its draft", async ({
  page,
}) => {
  await startCall(page, "Unsaved wrap-up fixture", "+14155550605");
  const seen = countStarts(page);
  await answer(page);
  await hangUp(page).click();
  const wrapUp = await addDetail(page);
  await wrapUp.getByLabel("Wrap-up note").fill("Draft I must not lose");
  await page.waitForTimeout(4000);
  expect(seen.starts).toBe(0);
  await expect(outcomeKeys(page)).toBeVisible();
  await expect(wrapUp.getByLabel("Wrap-up note")).toHaveValue(
    "Draft I must not lose",
  );
});

test("Start calling runs once while a request is pending", async ({ page }) => {
  await enter(page);
  if (await pauseCalling(page).isVisible()) await pauseCalling(page).click();
  let ready = 0;
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/rep/presence", async (route) => {
    if (route.request().postDataJSON()?.status === "ready") {
      ready++;
      await held;
    }
    await route.continue();
  });
  await startCalling(page).click();
  await expect.poll(() => ready).toBe(1);
  await expect(callStatus(page)).toContainText("Connecting audio");
  await expect(startCalling(page)).toBeHidden();
  release();
  await expect(pauseCalling(page)).toBeVisible();
  expect(ready).toBe(1);
});

test("pausing during a call stops the loop once that call is saved", async ({
  page,
}) => {
  await startCall(page, "Pause during call fixture", "+14155550606");
  const seen = countStarts(page);
  await answer(page);
  await pauseCalling(page).click();
  await expect(callStatus(page)).toContainText("Talking");
  await hangUp(page).click();
  await saveOutcome(page, "not_interested");
  await expect(startCalling(page)).toBeVisible();
  await page.waitForTimeout(3500);
  expect(seen.starts).toBe(0);
});

test("opt-out hangs up before the suppression sync completes", async ({
  page,
}) => {
  await startCall(page, "Opt-out fixture", "+14155550607");
  await answer(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/suppressions", async (route) => {
    await held;
    await route.continue();
  });
  const hungUp = page.waitForRequest(
    (r) => r.url().endsWith("/hangup") && r.method() === "POST",
    {
      timeout: 5000,
    },
  );
  await page.getByRole("button", { name: /opt.out/i }).click();
  try {
    await hungUp;
  } finally {
    release();
  }
  await expect(outcomeKeys(page)).toBeVisible();
});

test.describe("device in another zone", () => {
  test.use({ timezoneId: "Asia/Tokyo" });
  test("callback time is entered and shown in the lead's zone", async ({
    page,
  }) => {
    const { packet } = await startCall(page, "Zone fixture", "+14155550608");
    const zone = packet.lead.timezone as string;
    expect(zone).toBeTruthy();
    await answer(page);
    await pauseCalling(page).click();
    await hangUp(page).click();
    const wrapUp = await addDetail(page);
    await wrapUp
      .getByRole("button", { name: "Talked: callback requested", exact: true })
      .click();
    const input = page.getByLabel(/Confirmed callback time/);
    await expect(input).toBeVisible();
    await expect
      .soft(wrapUp.getByText(zone.replaceAll("_", " ")))
      .toBeVisible();
    await input.fill("2026-10-15T10:00");
    const sent = page.waitForRequest(
      (r) => r.url().endsWith("/disposition") && r.method() === "POST",
    );
    await wrapUp
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    expect((await sent).postDataJSON().callback_at).toBe(
      zonedInstant("2026-10-15T10:00", zone),
    );
    const nextWork = page.getByRole("region", { name: "Next work" });
    await expect(nextWork).toContainText(packet.lead.name);
    await expect(nextWork).toContainText(zone.replaceAll("_", " "));
  });
});

test("an open follow-up draft survives a refreshed task version", async ({
  page,
}) => {
  await startCall(page, "Follow-up draft fixture", "+14155550610");
  await answer(page);
  await pauseCalling(page).click();
  await hangUp(page).click();
  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/disposition") && r.request().method() === "POST",
  );
  await saveOutcome(page, "callback_requested");
  const call = await (await saved).json();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  const task = page.locator(`[data-call-id="${call.id}"]`);
  await task.getByLabel(/^Callback time/).fill("2026-10-23T09:15");
  // Another edit elsewhere advanced this task's version.
  await page.route("**/v1/workspace/tasks?*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.items = body.items.map((item: { call_id: string; version: number }) =>
      item.call_id === call.id ? { ...item, version: item.version + 1 } : item,
    );
    await route.fulfill({ response, json: body });
  });
  const reloaded = page.waitForResponse((r) =>
    r.url().includes("/v1/workspace/tasks?"),
  );
  await page.getByLabel("Follow-up history").selectOption("open");
  await reloaded;
  await expect(page.getByLabel("Follow-up history")).toHaveValue("open");
  await expect(task.getByLabel(/^Callback time/)).toHaveValue(
    "2026-10-23T09:15",
  );
});

test("a rejected session renews once instead of reporting the authority offline", async ({
  page,
}) => {
  await enter(page);
  if (await pauseCalling(page).isVisible()) await pauseCalling(page).click();
  await emptyQueue(page);
  let rejected = 0;
  await page.route("**/v1/workspace", async (route) => {
    if (rejected++ === 0)
      return route.fulfill({
        status: 401,
        json: { detail: "Revoked or expired session" },
      });
    await route.continue();
  });
  const renewed = page.waitForRequest((r) =>
    r.url().endsWith("/v1/local/session"),
  );
  await startCalling(page).click();
  await renewed;
  await expect(pauseCalling(page)).toBeVisible();
  await expect(page.getByText("Offline cache")).toHaveCount(0);
  await expect(page.getByText("Session expired")).toHaveCount(0);
});

test("a desktop opens its saved workspace without a browser session flag", async ({
  page,
}) => {
  await setUpAuthority(page);
  await page.addInitScript((url) => {
    Object.assign(window, {
      __TAURI_INTERNALS__: {
        transformCallback: () => 1,
        invoke: async (command: string) => {
          if (command === "backend_config")
            return { url, local_token: "e2e-local-token", simulation: false };
          if (command === "plugin:event|listen") return 1;
        },
      },
      __TAURI_EVENT_PLUGIN_INTERNALS__: { unregisterListener: () => {} },
    });
  }, AUTHORITY);
  await page.goto("/");
  await expect(
    page.getByRole("navigation").getByRole("button", { name: "Dialer" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start a local workspace" }),
  ).toHaveCount(0);
});

test("an unavailable local workspace reconnects without asking the rep to sign in", async ({
  page,
}) => {
  await enter(page);
  if (await pauseCalling(page).isVisible()) await pauseCalling(page).click();
  await emptyQueue(page);
  let available = false;
  let renewals = 0;
  let authorization = "";
  await page.route("**/v1/workspace", (route) => {
    // An old token remains expired after the local service can issue a new one.
    if (
      authorization &&
      route.request().headers().authorization === authorization
    )
      return route.continue();
    return route.fulfill({ status: 401, json: { detail: "Expired session" } });
  });
  await page.route("**/v1/local/session", async (route) => {
    renewals++;
    if (!available)
      return route.fulfill({
        status: 503,
        json: { detail: "Local service unavailable" },
      });
    const response = await route.fetch();
    authorization = `Bearer ${(await response.json()).access_token}`;
    await route.fulfill({ response });
  });
  await startCalling(page).click();
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "OpenDialer couldn’t reconnect" }),
  ).toBeVisible();
  expect(renewals).toBe(1);
  await expect(page.getByRole("button", { name: "Sign in again" })).toHaveCount(
    0,
  );
  available = true;
  await expect
    .poll(() => renewals, { timeout: 20_000 })
    .toBeGreaterThanOrEqual(2);
  await expect(
    page.getByText("OpenDialer couldn’t reconnect", { exact: false }),
  ).toHaveCount(0, { timeout: 20_000 });
  await expect(startCalling(page).or(pauseCalling(page))).toBeVisible();
});

test("a blocker fixed on another page offers a button that opens it", async ({
  page,
}) => {
  await page.route("**/v1/blockers", (route) =>
    route.fulfill({
      json: {
        blockers: [
          {
            code: "TELNYX_NOT_CONFIGURED",
            message: "Add your Telnyx API key in Settings.",
            fix: "settings",
          },
        ],
      },
    }),
  );
  await enter(page);
  if (await pauseCalling(page).isVisible()) await pauseCalling(page).click();
  await expect(callStatus(page)).toContainText("Add your Telnyx API key");
  await callStatus(page)
    .getByRole("button", { name: "Open Settings", exact: true })
    .click();
  await expect(page.locator(".breadcrumb strong")).toHaveText("Settings");
});
