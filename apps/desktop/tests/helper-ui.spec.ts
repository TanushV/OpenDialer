import { expect, test, type Page } from "./fixtures";
import { openMoreSettings } from "./navigation";
import {
  answer,
  callStatus,
  saveOutcome,
  finishCall,
  hangUp,
  leaveNoCall,
  prepareSimulationCall,
  startCalling,
} from "./simulation";

// The local authority is shared by every test in this file: a failed test
// must not leave its call active for the next one.
test.afterEach(async ({ page }) => leaveNoCall(page));

async function enter(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
}

/** Start calling and return the first call the loop places. */
async function firstCall(page: Page) {
  const started = page.waitForResponse((r) =>
    r.url().endsWith("/v1/calls/start"),
  );
  await startCalling(page).click();
  return await (await started).json();
}

const settle = (page: Page) =>
  page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );

test("a delayed helper read cannot bring back a previous call after the next one starts", async ({
  page,
}) => {
  await enter(page);
  // This journey calls twice: it brings its own second lead.
  await prepareSimulationCall(page, "Delayed helper fixture", "+14155550881", [
    { name: "Delayed helper next fixture", phone: "+14155550891" },
  ]);
  const original = await firstCall(page);
  await answer(page);
  let release!: () => void;
  let arrived!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  const pending = new Promise<void>((resolve) => {
    arrived = resolve;
  });
  await page.route(`**/v1/calls/${original.id}`, async (route) => {
    const response = await route.fetch();
    arrived();
    await delayed;
    await route.fulfill({ response });
  });
  try {
    await page.keyboard.press("F1");
    await pending;
    await hangUp(page).click();
    const next = page.waitForResponse((r) =>
      r.url().endsWith("/v1/calls/start"),
    );
    await saveOutcome(page, "not_interested");
    await page.getByRole("button", { name: /^Dial now/ }).click();
    const current = await (await next).json();
    expect(current.id).not.toBe(original.id);
    release();
    await page.unrouteAll({ behavior: "wait" });
    await settle(page);
    await expect(callStatus(page)).toContainText(/Dialing|Ringing/);
    await expect(page.getByRole("group", { name: "Save outcome" })).toHaveCount(
      0,
    );
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
    await finishCall(page);
  }
});

test("fixed helper remains visible, private controls work and no raw content is archived", async ({
  page,
}) => {
  const exposureRequests: Record<string, unknown>[] = [];
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().endsWith("/coach/feedback")
    )
      exposureRequests.push(request.postDataJSON());
  });
  await page.setViewportSize({ width: 1280, height: 720 });
  await enter(page);
  await prepareSimulationCall(page, "Helper Fixture", "+14155550175");
  await firstCall(page);
  await answer(page);
  await page.evaluate(() => window.scrollTo(0, 0));
  const opener = page
    .getByTestId("prepared-script")
    .locator(".script-reader > section p")
    .first();
  await expect(opener).toBeInViewport({ ratio: 1 });
  const openerBox = await opener.boundingBox();
  const hangupBox = await hangUp(page).boundingBox();
  expect(openerBox!.y + openerBox!.height).toBeLessThan(hangupBox!.y);
  await page
    .getByLabel("Simulation transcript")
    // Simulated cached knowledge needs a turn that matches an approved
    // example; a discovery card is no longer eligible for every turn.
    .fill("We have no budget for another provider right now.");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  const strip = page.getByTestId("live-coaching-strip");
  await expect(strip).toBeVisible();
  await page.mouse.wheel(0, 1200);
  await expect(strip).toBeInViewport();
  await expect(hangUp(page)).toBeInViewport();
  await page.getByRole("button", { name: "Mute tips", exact: true }).click();
  await expect(page.getByText("Automatic tips muted")).toBeVisible();
  await page.getByRole("button", { name: "Private help", exact: true }).click();
  await page
    .getByLabel("Private helper question")
    .fill("Which fact are we missing?");
  await page
    .getByRole("button", { name: "Ask privately", exact: true })
    .click();
  await expect(page.getByLabel("Private helper question")).toHaveValue("");
  const callView = /\/v1\/calls\/[^/]+$/;
  let reviewStatus = "model_suggestion";
  await page.route(callView, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (body.coach)
      body.coach = {
        ...body.coach,
        provider: "openrouter",
        meaning_review: { status: reviewStatus },
        alternatives: [
          "Which constraint matters most?",
          "What would make the next step worthwhile?",
        ],
      };
    await route.fulfill({ response, json: body });
  });
  for (const [status, label] of [
    ["model_suggestion", "Model suggestion"],
    ["model_reviewed", "Model-reviewed guidance"],
  ]) {
    reviewStatus = status;
    await page.keyboard.press("F1");
    await expect(strip.getByText(label, { exact: false })).toBeVisible();
    await expect(
      strip.getByText("Alternative: Which constraint matters most?", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(hangUp(page)).toBeInViewport();
  }
  await expect
    .poll(() => exposureRequests.some((r) => r.displayed === true))
    .toBe(true);
  // Rarer feedback reasons sit under the strip's small "More" menu.
  await strip.getByText("More", { exact: true }).click();
  await page
    .getByRole("button", { name: "I did not use this guidance", exact: true })
    .click();
  await expect(
    page.getByText("Guidance feedback saved", { exact: true }),
  ).toBeVisible();
  expect(exposureRequests.some((r) => r.used_by_rep === false)).toBe(true);
  expect(exposureRequests.every((r) => r.conversion === undefined)).toBe(true);
  await page.unrouteAll({ behavior: "wait" });
  await finishCall(page);
});

test("a lead is not dialed before its brief and talk track load", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Brief first fixture", "+14155550885");
  let starts = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/v1/calls/start")) starts++;
  });
  let release!: () => void;
  let arrived!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const packet = new Promise<void>((resolve) => {
    arrived = resolve;
  });
  await page.route(/\/v1\/leads\/[^/]+\/packet$/, async (route) => {
    const response = await route.fetch();
    arrived();
    await held;
    await route.fulfill({ response });
  });
  try {
    await startCalling(page).click();
    await packet;
    await page.waitForTimeout(500);
    expect(starts).toBe(0);
    release();
    await expect(page.getByTestId("lead-name")).toBeVisible();
    await expect.poll(() => starts).toBe(1);
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("settings display the authority cost register, including changed rates", async ({
  page,
}) => {
  await page.route("**/v1/admin/operating-policy", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.costs.voice.api_per_minute_usd = 0.0037;
    body.costs.dnc.prepaid_minimum_usd = 123;
    body.rates_checked_at = "2026-11-02";
    await route.fulfill({ response, json: body });
  });
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  )
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  await openMoreSettings(page, "Privacy and spending rules");
  await page.getByText("PAYG cost outline", { exact: true }).click();
  await expect(
    page.getByText("$0.0037/min API", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("$123 / 60,000 queries", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("Reference prices checked 2026-11-02", { exact: false }),
  ).toBeVisible();
});

test("a late hang-up answer cannot touch the next conversation's audio", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Delayed hangup fixture", "+14155550883", [
    { name: "Delayed hangup next fixture", phone: "+14155550893" },
  ]);
  const original = await firstCall(page);
  await answer(page);
  await page.evaluate(async () => {
    const path = "/src/telephony/client.ts";
    const { Telephony } = await import(path);
    const state = {
      id: "original-sdk-leg",
      stopped: [] as (string | undefined)[],
    };
    (window as any).audioBoundary = state;
    Object.defineProperty(Telephony.prototype, "activeIdentity", {
      configurable: true,
      get: () => state.id,
    });
    Telephony.prototype.emergencyHangup = async (id?: string) => {
      state.stopped.push(id);
    };
  });
  let firstArrived!: () => void, secondArrived!: () => void, sent!: () => void;
  let captured!: () => void, release!: () => void;
  const first = new Promise<void>((resolve) => {
    firstArrived = resolve;
  });
  const second = new Promise<void>((resolve) => {
    secondArrived = resolve;
  });
  const send = new Promise<void>((resolve) => {
    sent = resolve;
  });
  const gotResponse = new Promise<void>((resolve) => {
    captured = resolve;
  });
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let count = 0;
  await page.route(`**/v1/calls/${original.id}/hangup`, async (route) => {
    count++;
    if (count === 1) {
      firstArrived();
      await send;
      const response = await route.fetch();
      captured();
      await held;
      await route.fulfill({ response });
    } else {
      secondArrived();
      await gotResponse;
      await route.continue();
    }
  });
  try {
    await hangUp(page).click();
    await first;
    await page.locator(".dialer-body").click({ position: { x: 2, y: 2 } });
    await page.keyboard.press("Escape");
    await second;
    sent();
    await expect
      .poll(() =>
        page.evaluate(() => (window as any).audioBoundary.stopped.length),
      )
      .toBe(1);
    await page.evaluate(() => {
      (window as any).audioBoundary.id = "new-sdk-leg";
    });
    const nextStarted = page.waitForResponse((r) =>
      r.url().endsWith("/v1/calls/start"),
    );
    await saveOutcome(page, "not_interested");
    await page.getByRole("button", { name: /^Dial now/ }).click();
    const current = await (await nextStarted).json();
    release();
    await page.unrouteAll({ behavior: "wait" });
    await settle(page);
    await expect(callStatus(page)).toContainText(/Dialing|Ringing/);
    expect(
      await page.evaluate(() => (window as any).audioBoundary.stopped),
    ).toEqual(["original-sdk-leg"]);
    expect(current.id).not.toBe(original.id);
  } finally {
    sent();
    captured();
    release();
    await page.unrouteAll({ behavior: "wait" });
    await finishCall(page);
  }
});

test("a same-version stale help read cannot restore withdrawn guidance", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Withdrawn help fixture", "+14155550884");
  const original = await firstCall(page);
  await answer(page);
  let release!: () => void, received!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const arrived = new Promise<void>((resolve) => {
    received = resolve;
  });
  let count = 0;
  let capturedVersion: number, capturedTranscript: number;
  await page.route(`**/v1/calls/${original.id}`, async (route) => {
    const sequence = ++count;
    const response = await route.fetch();
    const body = await response.json();
    if (sequence === 1) {
      capturedVersion = body.version;
      capturedTranscript = body.transcript_version;
    }
    body.version = capturedVersion;
    body.transcript_version = capturedTranscript;
    body.coach =
      sequence === 1
        ? {
            id: "withdrawn-guidance",
            action: "SUGGEST",
            kind: "question",
            say_or_ask: "OBSOLETE ADVICE SHOULD NOT REAPPEAR",
            reason: "Synthetic earlier context",
            provider: "openrouter",
            remaining_ms: 60000,
            state_version: 1,
            evidence_segment_ids: [],
            meaning_review: { status: "model_suggestion" },
          }
        : null;
    if (sequence === 1) {
      received();
      await held;
    }
    await route.fulfill({ response, json: body });
  });
  try {
    await page.keyboard.press("F1");
    await arrived;
    await page.getByRole("button", { name: "Mute tips", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "Enable tips", exact: true }),
    ).toBeVisible();
    await page.evaluate(() => {
      (window as any).sawObsolete = false;
      const observer = new MutationObserver(() => {
        if (
          document.body.textContent?.includes(
            "OBSOLETE ADVICE SHOULD NOT REAPPEAR",
          )
        )
          (window as any).sawObsolete = true;
      });
      observer.observe(document.body, {
        subtree: true,
        childList: true,
        characterData: true,
      });
      (window as any).obsoleteObserver = observer;
    });
    release();
    await page.unrouteAll({ behavior: "wait" });
    await settle(page);
    expect(await page.evaluate(() => (window as any).sawObsolete)).toBe(false);
    await expect(
      page.getByText("OBSOLETE ADVICE SHOULD NOT REAPPEAR", { exact: true }),
    ).toHaveCount(0);
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
    await page.evaluate(() => (window as any).obsoleteObserver?.disconnect());
    await finishCall(page);
  }
});
