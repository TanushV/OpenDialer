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
  await page.locator("h1").click();
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

test("upload a Markdown script, preview sections, and preserve readable wording", async ({
  page,
}, testInfo) => {
  await enter(page);
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Script & learning" }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Script memory", exact: true }),
  ).toBeHidden();
  await page.getByRole("button", { name: "Upload or paste script" }).click();
  await page.getByLabel("Markdown or text file").setInputFiles({
    name: "sales.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "## Opener\nHi {name}, this is {rep} from {seller}.\n\n## Objection: busy\nWhen would be better?",
    ),
  });
  await page.getByRole("button", { name: "Preview sections" }).click();
  await expect(
    page.getByRole("heading", { name: "Objection: busy" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save script", exact: true }).click();
  await expect(
    page.getByText("Script saved. New calls will use this version."),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(
    page.getByText("When would be better?", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("script-learning-screen.png"),
    fullPage: true,
  });
});

test("a background script adoption cannot silently rebase an open editor", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page.getByRole("button", { name: "Upload or paste script" }).click();
  await page
    .getByLabel("Script text", { exact: true })
    .fill("## Opener\nMy original draft, {rep} from {seller}.");
  const previewRequest = page.waitForRequest((r) =>
    r.url().endsWith("/v1/admin/script/import"),
  );
  await page.getByRole("button", { name: "Preview sections" }).click();
  const sent = await previewRequest;
  await expect(
    page.getByRole("button", { name: "Save script", exact: true }),
  ).toBeVisible();
  const adopted = await page.request.post(sent.url(), {
    headers: { authorization: sent.headers()["authorization"] },
    data: {
      ...sent.postDataJSON(),
      markdown: "## Opener\nNew team script, {rep} from {seller}.",
      preview: false,
    },
  });
  expect(adopted.ok()).toBeTruthy();
  await expect(
    page.getByText(
      "The script changed while you were editing. Reopen the editor to review the latest version.",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save script", exact: true }).click();
  await expect(
    page.getByText("The script changed; reload before replacing it"),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Script text", exact: true }),
  ).toHaveValue(/My original draft/);
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
