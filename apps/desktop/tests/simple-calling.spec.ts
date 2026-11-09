import { expect, test } from "./fixtures";
import {
  callStatus,
  finishCall,
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
  const previewRequest = page.waitForRequest((r) =>
    r.url().endsWith("/v1/admin/script/import"),
  );
  await editor.fill(
    "# Opening\nHi {first_name}, this is {rep} from {seller}.\n\n# Screening\n{rep} from {seller}, about your process.\n\n# Discovery\n- How do you handle this today?\n\n# Objections\n## Busy\nWhen would be better?\n\n# Close\nWould a short call help?".replace(
      "Hi {first_name}",
      "My original draft, {first_name}",
    ),
  );
  const sent = await previewRequest;
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
