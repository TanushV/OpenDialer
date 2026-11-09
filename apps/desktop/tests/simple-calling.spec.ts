import { AUTHORITY, expect, test } from "./fixtures";
import {
  answer,
  callStatus,
  finishCall,
  hangUp,
  outcomeKeys,
  pauseCalling,
  prepareSimulationCall,
  startCalling,
  startDialing,
} from "./simulation";

async function enter(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
}

test("Space starts calling once and ignores editing and repeat presses", async ({
  page,
}, testInfo) => {
  await enter(page);
  await prepareSimulationCall(page, "Space shortcut", "+14155550771");
  let starts = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/v1/calls/start")) starts++;
  });
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByLabel("Search leads").focus();
  await page.keyboard.press("Space");
  expect(starts).toBe(0);
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await expect(startCalling(page)).toBeEnabled();
  await page.locator(".dialer-body").click({ position: { x: 5, y: 5 } });
  await page.keyboard.down("Space");
  await page.keyboard.down("Space");
  await page.keyboard.up("Space");
  await expect(callStatus(page)).toContainText(/Dialing|Ringing/);
  await expect.poll(() => starts).toBe(1);
  await expect(
    page.getByTestId("prepared-script").locator(".script-reader"),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("calling-screen.png"),
    fullPage: true,
  });
  await page.keyboard.press("KeyP");
  await expect(page.getByText("Calling stops after this call")).toBeVisible();
  await finishCall(page);
  expect(starts).toBe(1);
});

test("reload brings back the live call without dialing again", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Reload shortcut", "+14155550777");
  await startDialing(page);
  const lead = await page.getByTestId("lead-name").innerText();
  let starts = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/v1/calls/start")) starts++;
  });
  await page.reload();
  await expect(page.getByTestId("lead-name")).toHaveText(lead);
  await expect(callStatus(page)).toContainText(/Dialing|Ringing/);
  expect(starts).toBe(0);
  await finishCall(page);
});

test("open a Markdown script file, see it parsed, and keep readable wording", async ({
  page,
}, testInfo) => {
  await enter(page);
  await page.getByRole("button", { name: "Script", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Script", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Script memory", exact: true }),
  ).toBeHidden();
  await page.getByLabel("Open .md file").setInputFiles({
    name: "sales.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Opening\nHi {first_name}, this is {rep} from {seller}.\n\n# Screening\n{rep} from {seller}, about your process.\n\n# Discovery\n- How do you handle this today?\n\n# Objections\n## Busy\nWhen would be better?\n\n# Close\nWould a short call help?",
    ),
  });
  const outline = page.locator(".script-outline");
  await expect(
    outline.getByRole("heading", { name: "Objections" }),
  ).toBeVisible();
  await expect(outline).toContainText("Busy: When would be better?");
  await page.getByRole("button", { name: "Save script", exact: true }).click();
  await expect(page.getByText(/Script saved/).first()).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Script", exact: true }).click();
  await expect(page.locator(".script-outline")).toContainText(
    "When would be better?",
  );
  await page.screenshot({
    path: testInfo.outputPath("script-screen.png"),
    fullPage: true,
  });
});

test("a background script adoption cannot silently replace open edits", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Script", exact: true }).click();
  const editor = page.getByLabel("Script Markdown");
  await expect(editor).toHaveValue(/# Opening/);
  // Save is enabled once the script check answers, not when it is sent (a slow CI runner showed the gap).
  const previewResponse = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/script/import") &&
      r.request().postDataJSON()?.markdown?.includes("My original draft"),
  );
  await editor.fill(
    "# Opening\nHi {first_name}, this is {rep} from {seller}.\n\n# Screening\n{rep} from {seller}, about your process.\n\n# Discovery\n- How do you handle this today?\n\n# Objections\n## Busy\nWhen would be better?\n\n# Close\nWould a short call help?".replace(
      "Hi {first_name}",
      "My original draft, {first_name}",
    ),
  );
  const sent = (await previewResponse).request();
  await expect(
    page.getByRole("button", { name: "Save script", exact: true }),
  ).toBeEnabled();
  const adopted = await page.request.post(sent.url(), {
    headers: { authorization: sent.headers()["authorization"] },
    data: {
      ...sent.postDataJSON(),
      markdown:
        "# Opening\nHi {first_name}, this is {rep} from {seller}.\n\n# Screening\n{rep} from {seller}, about your process.\n\n# Discovery\n- How do you handle this today?\n\n# Objections\n## Busy\nWhen would be better?\n\n# Close\nWould a short call help?".replace(
          "Hi {first_name}",
          "New team script, {first_name}",
        ),
      preview: false,
    },
  });
  expect(adopted.ok()).toBeTruthy();
  await expect(
    page.getByText("A newer version was saved while you were editing."),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save script", exact: true }),
  ).toBeDisabled();
  await expect(editor).toHaveValue(/My original draft/);
  await page.getByRole("button", { name: "Load the newer version" }).click();
  await expect(editor).toHaveValue(/New team script/);
});

for (const pause of [false, true]) {
  test(`a skipped voicemail ${pause ? "stops when the rep pauses" : "dials the next lead by itself"}`, async ({
    page,
  }) => {
    await enter(page);
    await prepareSimulationCall(
      page,
      `Voicemail advance ${pause}`,
      pause ? "+14155550775" : "+14155550773",
      [
        {
          name: "Next scheduled prospect",
          phone: pause ? "+14155550776" : "+14155550774",
        },
      ],
    );
    let starts = 0;
    page.on("request", (request) => {
      if (request.url().endsWith("/v1/calls/start")) starts++;
    });
    await startDialing(page);
    await page
      .getByRole("button", { name: "Mailbox greeting", exact: true })
      .click();
    await expect(callStatus(page)).toContainText("Voicemail: skipping");
    await expect(page.getByRole("button", { name: /^Stay/ })).toBeVisible();
    await page
      .getByRole("button", { name: "Mailbox beep", exact: true })
      .click();
    if (pause) {
      // The skip waits at least 7 seconds after the answer (D6), then the carrier result is saved.
      await expect(callStatus(page)).toContainText("Skipped", {
        timeout: 30_000,
      });
      await pauseCalling(page).click();
      await expect(startCalling(page)).toBeVisible();
      // Pass the countdown: no hidden timer may place a call.
      await page.waitForTimeout(2500);
      expect(starts).toBe(1);
    } else {
      await expect.poll(() => starts).toBe(2);
      await finishCall(page);
    }
  });
}

test("Pause stays on screen after Start even while the workspace re-read is slow", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Slow refresh", "+14155550779");
  // The rep's own Start must not wait for a workspace re-read: a slow one used to swap Pause for
  // "Calling stops after this call" and back, and a click in between was lost.
  // A re-read that still says "paused" (it left before the rep's Start reached the server).
  let stale = true;
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (stale) body.user.presence.status = "paused";
    await route.fulfill({ response, json: body });
  });
  setTimeout(() => (stale = false), 6000);
  let paused = 0;
  page.on("request", (request) => {
    if (
      request.url().endsWith("/v1/rep/presence") &&
      request.postData()?.includes('"paused"')
    )
      paused++;
  });
  await startCalling(page).click();
  await expect(pauseCalling(page)).toBeVisible();
  for (let i = 0; i < 10; i++) {
    await expect(page.getByText("Calling stops after this call")).toBeHidden();
    await page.waitForTimeout(150);
  }
  await pauseCalling(page).click();
  await expect.poll(() => paused).toBe(1);
});

test("Hang up shows at once and a re-read from before the server saw it cannot bring the call back", async ({
  page,
}) => {
  // R9E.8: the rep's own key decides the screen until the server says the same.
  await enter(page);
  await prepareSimulationCall(page, "Held hang-up", "+14155550781");
  await startDialing(page);
  await answer(page);
  await expect(callStatus(page)).toContainText("Talking");
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  let hangups = 0;
  await page.route("**/v1/calls/*/hangup", async (route) => {
    hangups++;
    await held;
    await route.continue();
  });
  // Any re-read of the call meanwhile still says it is live.
  let stale = true;
  await page.route(/\/v1\/calls\/[^/]+$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (stale) body.state = "HUMAN";
    await route.fulfill({ response, json: body });
  });
  try {
    await page.keyboard.press("Escape");
    await expect(callStatus(page)).toContainText("You ended the call");
    await expect(hangUp(page)).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    await expect(callStatus(page)).toContainText("You ended the call");
    expect(hangups).toBe(1);
    stale = false;
    release();
    await expect(outcomeKeys(page)).toBeVisible();
  } finally {
    release();
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
});

test("an outcome pressed while the call is still ending is kept and saved once the end is confirmed", async ({
  page,
}) => {
  // R9E.8: the key the rep pressed is never dropped; it waits for the carrier's end, once.
  await enter(page);
  await prepareSimulationCall(page, "Ending outcome", "+14155550782");
  await startDialing(page);
  await answer(page);
  await pauseCalling(page).click();
  let ending = true;
  await page.route(/\/v1\/calls\/[^/]+(\/hangup)?$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (ending && body.state) body.state = "ENDING";
    await route.fulfill({ response, json: body });
  });
  const saves: string[] = [];
  page.on("request", (r) => {
    if (r.url().endsWith("/disposition")) saves.push(r.postData() || "");
  });
  try {
    await hangUp(page).click();
    await expect(callStatus(page)).toContainText("You ended the call");
    await outcomeKeys(page)
      .getByRole("button", { name: /Not interested$/ })
      .click();
    await expect(callStatus(page)).toContainText("Saving the outcome");
    await page.waitForTimeout(600);
    expect(saves).toHaveLength(0);
    ending = false;
    await expect.poll(() => saves.length).toBe(1);
    expect(JSON.parse(saves[0]).outcome).toBe("not_interested");
    await expect(startCalling(page)).toBeVisible();
  } finally {
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
});

test("Esc and the outcome keys work while a button has focus, as after any click on Windows", async ({
  page,
}) => {
  // Chromium (WebView2 on Windows) keeps focus on a clicked button; WebKit does not. Focus one on purpose.
  await enter(page);
  await prepareSimulationCall(page, "Focused button", "+14155550783");
  await startDialing(page);
  await answer(page);
  await page
    .getByRole("button", { name: "Simulate human answer", exact: true })
    .focus();
  await page.keyboard.press("Escape");
  await expect(callStatus(page)).toContainText("You ended the call");
  await outcomeKeys(page)
    .getByRole("button", { name: /Not interested$/ })
    .focus();
  await page.keyboard.press("Digit3");
  await expect(callStatus(page)).toContainText(
    /Saved|Start calling|Dialing|Ringing/,
  );
});

test("Pause pressed while the next lead is loading places no call and shows no error", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Pause while loading", "+14155550784");
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/v1/leads/*/packet", async (route) => {
    await held;
    await route.continue();
  });
  let starts = 0;
  page.on("request", (r) => {
    if (r.url().endsWith("/v1/calls/start")) starts++;
  });
  const packet = page.waitForRequest((r) => r.url().includes("/packet"));
  try {
    await startCalling(page).click();
    await packet;
    await pauseCalling(page).click();
    release();
    await expect(startCalling(page)).toBeVisible();
    await page.waitForTimeout(500);
    expect(starts).toBe(0);
    await expect(page.getByRole("alert")).toHaveCount(0);
  } finally {
    release();
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
});

test("Pause pressed while audio is connecting wins: nothing is reserved or dialed and the rep stays paused", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Pause while connecting", "+14155550785");
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  let holding = true;
  // Hold the step that marks the rep ready, so the screen stays on "Connecting audio" and it lands late.
  await page.route("**/v1/rep/presence", async (route) => {
    if (holding && route.request().postDataJSON()?.status === "ready") {
      holding = false;
      await held;
    }
    await route.continue();
  });
  const sent: string[] = [];
  page.on("request", (r) => {
    if (/queue\/next|calls\/start/.test(r.url())) sent.push(r.url());
  });
  try {
    await startCalling(page).click();
    await expect(callStatus(page)).toContainText("Connecting audio");
    await page.keyboard.press("KeyP");
    release();
    await expect(startCalling(page)).toBeVisible();
    await page.waitForTimeout(800);
    expect(sent).toEqual([]);
    // The server agrees: the rep is paused, even though "ready" reached it after "paused".
    const local = await page.request.post(`${AUTHORITY}/v1/local/session`, {
      headers: { "X-Local-Token": "e2e-local-token" },
      data: {},
    });
    const token = (await local.json()).access_token;
    const workspace = await page.request.get(`${AUTHORITY}/v1/workspace`, {
      headers: { authorization: `Bearer ${token}` },
    });
    expect((await workspace.json()).user.presence.status).toBe("paused");
  } finally {
    release();
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
});
