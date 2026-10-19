import { expect, test, type Page } from "@playwright/test";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "wait" });
});

test("late superseded settings load cannot replace a rep's template edits", async ({
  page,
}) => {
  await enter(page, false);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let loads = 0;
  let delivered = false;
  await page.route("**/v1/admin/operating-policy", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const index = ++loads;
    const response = await route.fetch();
    if (index === 1) await held;
    await route.fulfill({ response });
    if (index === 1) delivered = true;
  });
  try {
    await page.getByRole("button", { name: "Operations", exact: true }).click();
    const topic = page.getByLabel("Script topic", { exact: true });
    await expect(topic).toBeVisible();
    await topic.fill("Reviewed approval process");
    release();
    await expect.poll(() => delivered).toBeTruthy();
    await expect(topic).toHaveValue("Reviewed approval process");
  } finally {
    release();
  }
});

test("setup shows capability evidence and uses protected native credential entry", async ({
  page,
}) => {
  await enter(page, false);
  const requests: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "PUT" && r.url().endsWith("/admin/settings"))
      requests.push(r.postData() || "");
  });
  await page.evaluate(() => {
    const state = window as unknown as {
      __TAURI_INTERNALS__: {
        invoke: (name: string, args: unknown) => Promise<unknown>;
      };
      protectedCalls: unknown[];
    };
    state.protectedCalls = [];
    state.__TAURI_INTERNALS__ = {
      invoke: async (name, args) => {
        state.protectedCalls.push({ name, args });
        if (name === "webhook_tunnel_status")
          return {
            config: { enabled: false, host: "", user: "", remote_port: 18765 },
            state: "disabled",
          };
        if (name === "protected_provider_input")
          return { version: 1, configured: { openrouter_api_key: true } };
        return null;
      },
    };
  });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page
    .getByRole("button", { name: "Run preflight", exact: true })
    .click();
  await expect(page.getByTestId("capability-offline_callback")).toContainText(
    "Unavailable",
  );
  await expect(page.getByTestId("capability-live_helper")).toContainText(
    "Software verified",
  );
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await page
    .getByRole("button", { name: "OpenRouter API key", exact: true })
    .click();
  await expect(page.getByTestId("credential-openrouter_api_key")).toContainText(
    "Configured",
  );
  const calls = await page.evaluate(
    () =>
      (
        window as unknown as {
          protectedCalls: { name: string; args: unknown }[];
        }
      ).protectedCalls,
  );
  const call = calls.find((x) => x.name === "protected_provider_input")!;
  expect(call.args).toEqual({
    field: "openrouter_api_key",
    expectedVersion: 0,
  });
  expect(requests).toEqual([]);
  await page.evaluate(() => {
    delete (window as unknown as { __TAURI_INTERNALS__?: unknown })
      .__TAURI_INTERNALS__;
  });
});

test("settings preserve saved budgets and expose only supported treatments", async ({
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
        budget: {
          daily_limit_usd: 23,
          monthly_limit_usd: 321,
          number_purchase_limit_usd: 0,
        },
      },
    });
  });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await expect(page.getByLabel("Daily optional-work limit (USD)")).toHaveValue(
    "23",
  );
  await expect(page.getByLabel("Monthly limit (USD)")).toHaveValue("321");
  await page.getByRole("button", { name: "Policy & DNC", exact: true }).click();
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
  await page
    .getByRole("button", { name: "Save provider settings", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Active return call", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "End return call", exact: true }),
  ).toBeInViewport();
});

test("selected prepared lead survives navigation before dialing", async ({
  page,
}) => {
  await enter(page, false);
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
      await expect(
        page.getByRole("button", { name: "Settings", exact: true }),
      ).toBeDisabled();
      await expect(
        page.getByRole("button", { name: "Review", exact: true }),
      ).toBeDisabled();
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Policy & DNC", exact: true }).click();
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
