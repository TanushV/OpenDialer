import { expect, test, type Page } from "./fixtures";
import { chooseOutcome, finishCall, prepareSimulationCall } from "./simulation";
import { zonedInstant } from "../src/state/zones";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await finishCall(page);
  const pause = page.getByRole("button", { name: "Pause dialing" });
  if (await pause.isVisible()) await pause.click();
});

async function enter(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const choice = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await choice.isVisible()) await choice.click();
}

/** One owned fictional lead in a live simulated call; returns its packet. */
async function startCall(page: Page, name: string, phone: string) {
  await enter(page);
  await prepareSimulationCall(page, name, phone);
  const packet = page.waitForResponse((r) =>
    /\/v1\/leads\/[^/]+\/packet$/.test(r.url()),
  );
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  const body = await (await packet).json();
  const started = page.waitForResponse((r) =>
    r.url().endsWith("/v1/calls/start"),
  );
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  return { packet: body, call: await (await started).json() };
}

test("at 1024x720 the helper, script opener, hangup and pause-after stay visible together and focus survives a live update", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 720 });
  const { call } = await startCall(page, "Small window rep", "+14155550601");
  const visible = [
    page.getByTestId("live-coaching-strip"),
    page
      .getByTestId("prepared-script")
      .locator(".script-reader > section p")
      .first(),
    page.getByRole("button", { name: "Hang up", exact: true }),
    page.getByRole("button", { name: /opt.out/i }),
    page.getByRole("checkbox", { name: "Pause after call" }),
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
  const local = await page.request.post(
    "http://127.0.0.1:8765/v1/local/session",
    { headers: { "X-Local-Token": "e2e-local-token" }, data: {} },
  );
  const token = (await local.json()).access_token;
  const turn = await page.request.post(
    `http://127.0.0.1:8765/v1/calls/${call.id}/transcript`,
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

test("stage estimates say prior only or show their support, never a bare pickup percentage", async ({
  page,
}) => {
  let priorOnly = true;
  await page.route("**/v1/leads/*/packet", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const support = (known: number) => ({
      cell_known: known,
      cell_successes: 0,
      cell_unknown: 0,
      conditioning_excluded: 0,
      outside_known: 0,
      observed_interval: [0, 1],
      interval_kind: "observed_rate_wilson",
    });
    body.statistical_guidance = {
      ...body.statistical_guidance,
      human_probability: 0.42,
      right_party_given_human: 0.5,
      conversion_given_right_party: 0.1,
      prior_only: priorOnly,
      stage_support: {
        human: support(priorOnly ? 0 : 12),
        right_party: support(0),
        conversion: support(priorOnly ? 0 : 3),
      },
    };
    await route.fulfill({ response, json: body });
  });
  await enter(page);
  await prepareSimulationCall(page, "Estimate fixture", "+14155550611");
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  const stats = page.getByTestId("statistical-guidance");
  await expect(stats).toContainText("prior only");
  await expect(stats).not.toContainText("42%");
  await expect(stats).not.toContainText("estimated pickup");
  priorOnly = false;
  await page.getByRole("button", { name: "Pause dialing" }).click();
  await page.reload();
  await page.getByRole("button", { name: "Go ready" }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await expect(stats).toContainText("Pickup 42% (n 12)");
  await expect(stats).toContainText("Right party: prior only");
});

test("review notes start empty and the AI summary stays read-only", async ({
  page,
}) => {
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.human_notes = null;
    body.notes.summary = "AI drafted summary of the call.";
    await route.fulfill({ response, json: body });
  });
  const { packet } = await startCall(
    page,
    "Review notes fixture",
    "+14155550602",
  );
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "no_answer");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page.getByRole("button", { name: "Pause dialing" }).click();
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

test("per-call review drafts and evidence reset when switching calls", async ({
  page,
}) => {
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.notes.objections = [
      { text: "Synthetic objection", evidence: ["synthetic-segment"] },
    ];
    await route.fulfill({ response, json: body });
  });
  await enter(page);
  await prepareSimulationCall(page, "Switch review fixture", "+14155550603");
  const names: string[] = [];
  for (let index = 0; index < 2; index++) {
    const reserved = page.waitForResponse((r) =>
      r.url().endsWith("/v1/queue/next"),
    );
    await page
      .getByRole("button", { name: "Get next lead", exact: true })
      .click();
    names.push((await (await reserved).json()).lead.name);
    await page.getByRole("button", { name: "Start call", exact: true }).click();
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    await chooseOutcome(page, "no_answer");
    await page
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Save outcome", exact: true }),
    ).toBeHidden();
  }
  await page.getByRole("button", { name: "Pause dialing" }).click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: `Review call ${names[0]}`, exact: true })
    .first()
    .click();
  await page.getByRole("button", { name: "synthetic-segment" }).click();
  await page.getByLabel("Comment", { exact: true }).fill("Draft for call A");
  await page.getByRole("button", { name: "All conversations" }).click();
  await page
    .getByRole("button", { name: `Review call ${names[1]}`, exact: true })
    .first()
    .click();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue("");
  await page.getByLabel("Review action").selectOption("SCORED");
  await expect(
    page.getByText(/Select supporting transcript moments/),
  ).toBeVisible();
});

test("a saved outcome stays saved when the follow-up refresh fails", async ({
  page,
}) => {
  await startCall(page, "Refresh failure fixture", "+14155550604");
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "no_answer");
  await page.route("**/v1/leads/*/packet", (route) =>
    route.fulfill({ status: 500, json: { detail: "Synthetic packet outage" } }),
  );
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await expect(
    page
      .getByText("Outcome saved. Analysis continues in the background.", {
        exact: true,
      })
      .or(page.getByText(/Outcome saved\. Refreshing/)),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save outcome", exact: true }),
  ).toBeHidden();
  await expect(page.getByText(/Synthetic packet outage/)).toBeVisible();
  await expect(page.getByText(/^Outcome saved\./)).toBeVisible();
});

test("an unsaved wrap-up blocks the next lead with a clear prompt", async ({
  page,
}) => {
  await startCall(page, "Unsaved wrap-up fixture", "+14155550605");
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await page.getByLabel("Wrap-up note").fill("Draft I must not lose");
  await expect(
    page.getByRole("button", { name: "Get next lead", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("Save this call's outcome before the next lead."),
  ).toBeVisible();
  await expect(page.getByLabel("Wrap-up note")).toHaveValue(
    "Draft I must not lose",
  );
});

test("Go ready runs once while a request is pending", async ({ page }) => {
  await enter(page);
  const pause = page.getByRole("button", { name: "Pause dialing" });
  if (await pause.isVisible()) await pause.click();
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
  const go = page.getByRole("button", { name: /Go ready|Getting ready/ });
  await go.click();
  await expect.poll(() => ready).toBe(1);
  await expect(go).toBeDisabled();
  await go.click({ force: true }).catch(() => undefined);
  release();
  await expect(pause).toBeVisible();
  expect(ready).toBe(1);
});

test("pause after call releases the calling session once the call ends", async ({
  page,
}) => {
  await startCall(page, "Pause after fixture", "+14155550606");
  await page.getByRole("checkbox", { name: "Pause after call" }).check();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await expect(page.getByRole("button", { name: "Go ready" })).toBeVisible();
  await chooseOutcome(page, "no_answer");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save outcome", exact: true }),
  ).toBeHidden();
  // The released session no longer holds the consumed reservation's identity.
  await expect(page.locator(".call-dock")).not.toContainText(
    "Your calling identity",
  );
});

test("opt-out hangs up before the suppression sync completes", async ({
  page,
}) => {
  await startCall(page, "Opt-out fixture", "+14155550607");
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
    { timeout: 5000 },
  );
  await page.getByRole("button", { name: /opt.out/i }).click();
  try {
    await hungUp;
  } finally {
    release();
  }
  await expect(
    page.getByRole("button", { name: "Save outcome", exact: true }),
  ).toBeVisible();
});

test.describe("device in another zone", () => {
  test.use({ timezoneId: "Asia/Tokyo" });
  test("callback time is entered and shown in the lead's zone", async ({
    page,
  }) => {
    const { packet } = await startCall(page, "Zone fixture", "+14155550608");
    const zone = packet.lead.timezone as string;
    expect(zone).toBeTruthy();
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    await chooseOutcome(page, "callback_requested");
    const input = page.getByLabel(/Confirmed callback time/);
    await expect(input).toBeVisible();
    await expect
      .soft(page.locator(".wrap-up").getByText(zone.replaceAll("_", " ")))
      .toBeVisible();
    await input.fill("2026-10-15T10:00");
    const sent = page.waitForRequest(
      (r) => r.url().endsWith("/disposition") && r.method() === "POST",
    );
    await page
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    expect((await sent).postDataJSON().callback_at).toBe(
      zonedInstant("2026-10-15T10:00", zone),
    );
    await page.getByRole("button", { name: "Pause dialing" }).click();
    const nextWork = page.getByRole("region", { name: "Next work" });
    await expect(nextWork).toContainText(packet.lead.name);
    await expect(nextWork).toContainText(zone.replaceAll("_", " "));
  });
});

test("an open follow-up draft survives a refreshed task version", async ({
  page,
}) => {
  await startCall(page, "Follow-up draft fixture", "+14155550610");
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "callback_requested");
  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/disposition") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  const call = await (await saved).json();
  await page.getByRole("button", { name: "Pause dialing" }).click();
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
  const pause = page.getByRole("button", { name: "Pause dialing" });
  if (await pause.isVisible()) await pause.click();
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
  await page.getByRole("button", { name: "Go ready" }).click();
  await renewed;
  await expect(pause).toBeVisible();
  await expect(page.getByText("Offline cache")).toHaveCount(0);
  await expect(page.getByText("Session expired")).toHaveCount(0);
});

test("a session that cannot renew asks the rep to sign in again", async ({
  page,
}) => {
  await enter(page);
  const pause = page.getByRole("button", { name: "Pause dialing" });
  if (await pause.isVisible()) await pause.click();
  const reject = (route: import("@playwright/test").Route) =>
    route.fulfill({
      status: 401,
      json: { detail: "Revoked or expired session" },
    });
  await page.route("**/v1/workspace", reject);
  await page.route("**/v1/local/session", reject);
  await page.getByRole("button", { name: "Go ready" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Session expired" }),
  ).toBeVisible();
  await expect(page.getByText("Offline cache")).toHaveCount(0);
  await page.unrouteAll({ behavior: "wait" });
  await page.getByRole("button", { name: "Sign in again" }).click();
  await expect(page.getByText("Session expired")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /Go ready|Pause dialing/ }),
  ).toBeVisible();
});

test("helper feedback offers useful, not useful and wrong without expanding", async ({
  page,
}) => {
  await startCall(page, "Feedback fixture", "+14155550609");
  await page.getByRole("button", { name: "Simulate human answer" }).click();
  await page
    .getByLabel("Simulation transcript")
    .fill("We need approval from the committee first.");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  await page.keyboard.press("F1");
  const strip = page.getByTestId("live-coaching-strip");
  for (const name of [
    "Guidance was useful",
    "Guidance was not useful",
    "Guidance was wrong",
  ])
    await expect(
      strip.getByRole("button", { name, exact: true }),
    ).toBeVisible();
  await expect(
    strip.getByRole("button", { name: "Guidance arrived too late" }),
  ).toBeHidden();
});
