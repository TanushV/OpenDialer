import { expect, test, type Page } from "@playwright/test";

async function enter(page: Page, connected: boolean) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  await expect(
    page
      .getByTitle("Sign out")
      .or(page.getByRole("button", { name: "Connect to a server" })),
  ).toBeVisible();
  if (connected) {
    if (await page.getByTitle("Sign out").isVisible())
      await page.getByTitle("Sign out").click();
    await page.getByRole("button", { name: "Connect to a server" }).click();
    await page.getByLabel("Server URL").fill("http://127.0.0.1:8766");
    await page.getByLabel("Email").fill("team@example.test");
    await page
      .getByLabel("Password", { exact: true })
      .fill("Connected-test-password-123");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  } else if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  ) {
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  }
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
}

for (const connected of [false, true]) {
  test(`authority_switch_scope ${connected ? "postgres" : "local"}`, async ({
    page,
  }) => {
    await enter(page, connected);
    await page.getByRole("button", { name: "Leads", exact: true }).click();
    await page
      .getByLabel("CSV lead data")
      .fill(
        `name,phone,company,timezone,region\nAuthority,+1415555015${connected ? "1" : "2"},Example,America/Los_Angeles,415`,
      );
    await page
      .getByRole("button", { name: "Import leads", exact: true })
      .click();
    await expect(page.getByText(/1 imported/)).toBeVisible();
    await page.getByRole("button", { name: "Dialer", exact: true }).click();
    if (await page.getByRole("button", { name: "Go ready" }).isVisible())
      await page.getByRole("button", { name: "Go ready" }).click();
    await page
      .getByRole("button", { name: "Get next lead", exact: true })
      .click();
    await page.getByRole("button", { name: "Start call", exact: true }).click();
    try {
      await expect(
        page.getByRole("button", { name: "Hang up", exact: true }),
      ).toBeEnabled();
      await expect(page.getByTitle("Sign out")).toBeDisabled();
    } finally {
      await page.getByRole("button", { name: "Hang up", exact: true }).click();
      if (await page.getByLabel("Disposition").isVisible()) {
        await page.getByLabel("Disposition").selectOption("no_answer");
        await page
          .getByRole("button", { name: "Save outcome", exact: true })
          .click();
      }
      if (
        await page
          .getByRole("button", { name: "Pause dialing", exact: true })
          .isVisible()
      )
        await page
          .getByRole("button", { name: "Pause dialing", exact: true })
          .click();
    }
    await expect(page.getByTitle("Sign out")).toBeEnabled();
  });
  for (const width of [1024, 1280]) {
    test(`small_window_keyboard_journey ${connected ? "postgres" : "local"} ${width}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 720 });
      await enter(page, connected);
      await page.getByRole("button", { name: "Leads", exact: true }).click();
      await page
        .getByLabel("CSV lead data")
        .fill(
          `name,phone,company,timezone,region\nCompletion ${width},+1415555${width === 1024 ? "0131" : "0132"},Example,America/Los_Angeles,415`,
        );
      await page
        .getByRole("button", { name: "Import leads", exact: true })
        .click();
      await expect(page.getByText(/1 imported/)).toBeVisible();
      await page.getByRole("button", { name: "Dialer", exact: true }).click();
      if (await page.getByRole("button", { name: "Go ready" }).isVisible())
        await page.getByRole("button", { name: "Go ready" }).click();
      await page
        .getByRole("button", { name: "Get next lead", exact: true })
        .click();
      await expect(page.getByTestId("prospect-brief")).toBeVisible();
      await page
        .getByRole("button", { name: "Start call", exact: true })
        .click();
      try {
        await page.evaluate(() => window.scrollTo(0, 0));
        await expect(
          page.getByTestId("prepared-script").locator("blockquote"),
        ).toBeInViewport({ ratio: 1 });
        await expect(page.getByTestId("live-coaching-strip")).toBeInViewport();
        await expect(
          page.getByRole("button", { name: "Hang up", exact: true }),
        ).toBeInViewport();
        if (
          await page
            .getByRole("button", { name: "Mute tips", exact: true })
            .isVisible()
        )
          await page
            .getByRole("button", { name: "Mute tips", exact: true })
            .click();
        await page
          .getByRole("button", { name: "Private help", exact: true })
          .focus();
        const helpRequest = page.waitForRequest(
          (r) => r.url().endsWith("/coach") && r.method() === "POST",
        );
        await page.keyboard.press("F1");
        await helpRequest;
        await page
          .getByRole("button", { name: "Private help", exact: true })
          .click();
        const question = page.getByLabel("Private helper question");
        await expect(question).toBeFocused();
        await question.fill("Which approved fact is missing?");
        // Asynchronous workspace updates must not steal the rep's focus/text.
        await page
          .getByLabel("Simulation transcript")
          .fill("The committee needs a clearer approval process.");
        await page.getByRole("button", { name: "Add prospect turn" }).click();
        await question.focus();
        await expect(question).toHaveValue("Which approved fact is missing?");
        await page.keyboard.press("Escape");
        await expect(question).toBeHidden();
        await expect(
          page.getByRole("button", { name: "Private help", exact: true }),
        ).toBeFocused();
        await page.route("**/coach", (route) =>
          route.fulfill({
            status: 503,
            json: { detail: "Synthetic helper outage" },
          }),
        );
        await page.keyboard.press("F1");
        await page
          .getByRole("button", { name: "Private help", exact: true })
          .click();
        await question.fill("Help me clarify the next step.");
        await page
          .getByRole("button", { name: "Ask privately", exact: true })
          .click();
        await expect(
          page.getByRole("button", { name: "Hang up", exact: true }),
        ).toBeEnabled();
        await expect(
          page.getByRole("button", { name: /opt.out/i }),
        ).toBeInViewport();
      } finally {
        if (
          await page
            .getByRole("button", { name: "Hang up", exact: true })
            .isVisible()
        )
          await page
            .getByRole("button", { name: "Hang up", exact: true })
            .click();
        if (await page.getByLabel("Disposition").isVisible()) {
          await page.getByLabel("Disposition").selectOption("no_answer");
          await page
            .getByRole("button", { name: "Save outcome", exact: true })
            .click();
        }
        if (
          await page
            .getByRole("button", { name: "Pause dialing", exact: true })
            .isVisible()
        )
          await page
            .getByRole("button", { name: "Pause dialing", exact: true })
            .click();
      }
      await page.reload();
      await expect(
        page.getByRole("button", { name: "Go ready" }),
      ).toBeVisible();
    });
  }
}
