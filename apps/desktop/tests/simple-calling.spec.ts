import { expect, test } from "./fixtures";
import { prepareSimulationCall, finishCall } from "./simulation";

async function enter(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
}

test("Space starts one scheduler-selected call and ignores editing and repeat presses", async ({
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
  await expect(
    page
      .getByRole("button", { name: "Next call Space", exact: true })
      .or(page.getByRole("button", { name: "Start call", exact: true })),
  ).toBeEnabled();
  await page.getByTestId("prepared-script").locator(".card-eyebrow").click();
  await page.keyboard.down("Space");
  await page.keyboard.down("Space");
  await page.keyboard.up("Space");
  await expect(
    page.getByRole("button", { name: "Hang up", exact: true }),
  ).toBeVisible();
  expect(starts).toBe(1);
  await page.keyboard.press("Space");
  expect(starts).toBe(1);
  await expect(
    page.getByTestId("prepared-script").locator(".script-reader"),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("calling-screen.png"),
    fullPage: true,
  });
  await finishCall(page);
});

test("reload restores the reserved lead and Space starts that reservation once", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(page, "Reserved shortcut", "+14155550777");
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Start call", exact: true }),
  ).toBeEnabled();
  const lead = await page.getByTestId("lead-name").innerText();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Start call", exact: true }),
  ).toBeEnabled();
  await expect(page.getByTestId("lead-name")).toHaveText(lead);
  let starts = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/v1/calls/start")) starts++;
  });
  await page.getByTestId("prepared-script").locator(".card-eyebrow").click();
  await page.keyboard.press("Space");
  await expect(
    page.getByRole("button", { name: "Hang up", exact: true }),
  ).toBeVisible();
  expect(starts).toBe(1);
  await expect(page.getByTestId("lead-name")).toHaveText(lead);
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

test("pause after call takes precedence over automatic voicemail advance", async ({
  page,
}) => {
  await enter(page);
  await prepareSimulationCall(
    page,
    "Pause before automatic next",
    "+14155550772",
  );
  await page
    .getByRole("checkbox", {
      name: "Next scheduled call automatically after voicemail ends",
    })
    .check();
  await page.getByRole("checkbox", { name: "Pause after call" }).check();
  await expect(
    page.getByRole("checkbox", {
      name: "Next scheduled call automatically after voicemail ends",
    }),
  ).toBeDisabled();
});

for (const cancel of [false, true]) {
  test(`voicemail advance ${cancel ? "can be cancelled" : "requests one next scheduled call"}`, async ({
    page,
  }) => {
    await enter(page);
    await prepareSimulationCall(
      page,
      `Voicemail advance ${cancel}`,
      cancel ? "+14155550775" : "+14155550773",
      [
        {
          name: "Next scheduled prospect",
          phone: cancel ? "+14155550776" : "+14155550774",
        },
      ],
    );
    let starts = 0;
    page.on("request", (request) => {
      if (request.url().endsWith("/v1/calls/start")) starts++;
    });
    const automatic = page.getByRole("checkbox", {
      name: "Next scheduled call automatically after voicemail ends",
    });
    await automatic.check();
    await page
      .getByRole("button", { name: "Next call Space", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Mailbox greeting", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Mailbox beep", exact: true })
      .click();
    await expect(page.getByText(/Mailbox reached\./)).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Start monitored voicemail" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Start verified detached workflow" }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    await expect(
      page.getByText("Next scheduled call in five seconds."),
    ).toBeVisible();
    if (cancel) {
      await page.getByRole("button", { name: "Cancel", exact: true }).click();
      await expect(automatic).not.toBeChecked();
      // Pass the actual five-second window: no hidden timer may place a call.
      await page.waitForTimeout(5500);
      expect(starts).toBe(1);
    } else {
      await expect.poll(() => starts).toBe(2);
      await automatic.uncheck();
      await expect(
        page.getByRole("button", { name: "Hang up", exact: true }),
      ).toBeVisible();
      await finishCall(page);
      expect(starts).toBe(2);
    }
  });
}
