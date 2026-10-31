import { expect, test, type Page } from "./fixtures";
import { openMoreSettings, openSettingsCard } from "./navigation";
import { importLead, finishCall, waitForPreparation } from "./simulation";

test("setup saves a write-only live help key", async ({ page }) => {
  await enter(page, false);
  const requests: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "PUT" && r.url().endsWith("/admin/settings"))
      requests.push(r.postData() || "");
  });
  const settingsRead = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/settings") &&
      r.request().method() === "GET" &&
      r.ok(),
  );
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const settingsVersion = (await (await settingsRead).json()).version;
  await page
    .getByRole("button", { name: "Check readiness", exact: true })
    .click();
  await expect(page.getByRole("region", { name: "Readiness" })).toBeVisible();
  await openSettingsCard(page, "AI");
  const field = page.getByTestId("credential-openrouter_api_key");
  const name = "Live help key (OpenRouter, Cerebras)";
  await field.getByLabel(name).fill("sk-or-test-only");
  await field
    .getByRole("button", { name: `Save ${name}`, exact: true })
    .click();
  await expect(field).toContainText("Configured");
  await expect(field.getByLabel(name)).toHaveValue("");
  expect(requests).toHaveLength(1);
  expect(JSON.parse(requests[0])).toMatchObject({
    openrouter_api_key: "sk-or-test-only",
    expected_version: settingsVersion,
  });
});

test("settings show the per-list budget and expose only supported treatments", async ({
  page,
}) => {
  await enter(page, false);
  await page.route("**/v1/admin/settings", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({
      response,
      json: {
        ...data,
        budget: { per_list_usd: 23, number_purchase_limit_usd: 0 },
      },
    });
  });
  await openMoreSettings(page, "AI budget");
  await expect(page.getByLabel("AI budget per lead list (USD)")).toHaveValue(
    "23",
  );
  await openMoreSettings(page, "Do-not-call checks and consent");
  await expect(
    page.getByRole("checkbox", { name: "Recording", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: "Messaging", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: "Transcription", exact: true }),
  ).toBeVisible();
});

test("inbound arrival exposes call controls while an admin page is open", async ({
  page,
}) => {
  await enter(page, false);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.tasks.push({
      id: "incoming-from-settings",
      version: 1,
      inbound: true,
      inbound_state: "connected",
      routed_rep_id: data.user.id,
      callback_phone: "+14155550111",
      lead_id: null,
      status: "open",
      action: "Return call",
      route: "responsible_rep",
    });
    await route.fulfill({ response, json: data });
  });
  await openSettingsCard(page, "Calling");
  const active = page.getByRole("region", {
    name: "Active return call",
    exact: true,
  });
  // Saving reloads the workspace. A background refresh can deliver the arrival first, which
  // replaces this page with the call controls before the click lands; either way they show.
  await page
    .getByRole("button", { name: "Save Telnyx settings", exact: true })
    .click({ timeout: 5_000 })
    .catch(async (error) => {
      if (!(await active.isVisible())) throw error;
    });
  await expect(active).toBeVisible();
  await expect(
    page.getByRole("button", { name: "End return call", exact: true }),
  ).toBeInViewport();
});

test("selected prepared lead survives navigation before dialing", async ({
  page,
}) => {
  await enter(page, false);
  await importLead(page, "Selected lead fixture", "+14155550701");
  if (await page.getByRole("button", { name: "Go ready" }).isVisible())
    await page.getByRole("button", { name: "Go ready" }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Start call", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Start call", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
});

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
        `name,phone,company,timezone,region\nAuthority,+1415555015${connected ? "1" : "2"},Authority ${connected ? "hosted" : "local"} Co,America/Los_Angeles,415`,
      );
    await page
      .getByRole("button", { name: "Import leads", exact: true })
      .click();
    await expect(page.getByText(/1 imported/)).toBeVisible();
    await waitForPreparation(page);
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
      await expect(
        page.getByRole("button", { name: "Settings", exact: true }),
      ).toBeDisabled();
      await expect(
        page.getByRole("button", { name: "Review", exact: true }),
      ).toBeDisabled();
    } finally {
      await finishCall(page);
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
          `name,phone,company,timezone,region\nCompletion ${width},+1415555${width === 1024 ? "0131" : "0132"},Completion ${width} ${connected ? "hosted" : "local"} Co,America/Los_Angeles,415`,
        );
      await page
        .getByRole("button", { name: "Import leads", exact: true })
        .click();
      await expect(page.getByText(/1 imported/)).toBeVisible();
      await waitForPreparation(page);
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
          page
            .getByTestId("prepared-script")
            .locator(".script-reader > section p")
            .first(),
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
        // Deliver the asynchronous update while the rep is already typing;
        // refocusing after delivery would not test focus preservation.
        let releaseTurn!: () => void;
        let turnArrived!: () => void;
        const arrived = new Promise<void>((resolve) => {
          turnArrived = resolve;
        });
        const release = new Promise<void>((resolve) => {
          releaseTurn = resolve;
        });
        await page.route("**/v1/calls/*/transcript", async (route) => {
          turnArrived();
          await release;
          await route.continue();
        });
        await page
          .getByLabel("Simulation transcript")
          .fill("The committee needs a clearer approval process.");
        await page.getByRole("button", { name: "Add prospect turn" }).click();
        await arrived;
        await question.focus();
        const updated = page.waitForResponse(
          (r) =>
            r.url().endsWith("/v1/workspace") && r.request().method() === "GET",
        );
        releaseTurn();
        await updated;
        await expect(page.getByLabel("Simulation transcript")).toHaveValue("");
        await page.evaluate(
          () =>
            new Promise<void>((resolve) =>
              requestAnimationFrame(() =>
                requestAnimationFrame(() => resolve()),
              ),
            ),
        );
        await expect(question).toBeFocused();
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
        await finishCall(page);
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

test("seller DNC authorization saves with its loaded version and stable request identity", async ({
  page,
}) => {
  await enter(page, false);
  let saved: Record<string, unknown> | null = null;
  await page.route("**/v1/admin/settings", async (route) => {
    if (route.request().method() === "PUT") {
      saved = route.request().postDataJSON();
      await route.fulfill({ json: { version: 5, configured: {} } });
      return;
    }
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: { ...(await response.json()), version: 4 },
    });
  });
  await openMoreSettings(page, "Privacy and spending rules");
  await page
    .getByText("Seller authorization and source freshness", { exact: true })
    .click();
  await page
    .getByLabel("Seller access reference")
    .fill("Reviewed fixture source");
  await page.getByLabel("Covered prospect area codes").fill("415, 212");
  await page
    .getByLabel("Authorization expiry (ISO timestamp)")
    .fill("2026-12-01T00:00:00Z");
  await page
    .getByLabel("Registry source refresh (ISO timestamp)")
    .fill("2026-10-14T00:00:00Z");
  await page
    .getByLabel("I verified this seller's access and the provider's source")
    .check();
  await page
    .getByRole("button", { name: "Save seller authorization", exact: true })
    .click();
  await expect(
    page.getByText("Seller authorization saved", { exact: true }),
  ).toBeVisible();
  expect(saved).toMatchObject({
    expected_version: 4,
    request_id: expect.stringMatching(/^[a-f0-9-]{36}$/),
    dnc_seller_authorization: { npas: ["415", "212"] },
  });
});
