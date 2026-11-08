import { expect, test, type Page, setUpAuthority } from "./fixtures";
import { openScreening } from "./navigation";

test("saving Telnyx saves the pasted key and connection together", async ({
  page,
}) => {
  await enter(page);
  await openSettings(page);
  await openCard(page, "Calling");
  await page
    .getByLabel("Telnyx API key", { exact: true })
    .fill("synthetic-onboarding-key");
  const connection = `synthetic-connection-${Date.now()}`;
  await page.getByLabel("Voice API connection ID").fill(connection);
  const saved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/settings") && r.request().method() === "PUT",
  );
  await page
    .getByRole("button", { name: "Save Telnyx settings", exact: true })
    .click();
  const response = await saved;
  expect(response.ok()).toBeTruthy();
  expect(response.request().postDataJSON()).toMatchObject({
    telnyx_api_key: "synthetic-onboarding-key",
    telnyx_connection_id: connection,
  });
  await expect(page.getByLabel("Telnyx API key", { exact: true })).toHaveValue(
    "",
  );
});

test("a failed script load offers retry instead of a permanently disabled editor", async ({
  page,
}) => {
  await page.route("**/v1/optimization/script", async (route) =>
    !(await page.evaluate(
      () => document.documentElement.dataset.scriptRetried === "yes",
    ))
      ? route.fulfill({
          status: 503,
          json: { detail: "Temporarily unavailable" },
        })
      : route.continue(),
  );
  await enter(page);
  await page.getByRole("button", { name: "Script", exact: true }).click();
  const retry = page.getByRole("button", { name: "Try again" });
  await expect(retry).toBeVisible();
  // Background optimization events can reload this panel. Keep the injected outage
  // active until the actual user click, so automatic recovery cannot remove the target.
  await retry.evaluate((button) =>
    button.addEventListener(
      "click",
      () => {
        document.documentElement.dataset.scriptRetried = "yes";
      },
      { once: true },
    ),
  );
  await retry.click();
  await expect(page.getByLabel("Script Markdown")).toBeVisible();
});

test("provider edits can clear saved optional values", async ({ page }) => {
  await enter(page);
  await openSettings(page);
  await openCard(page, "Calling");
  const reference = page.getByLabel("Webhook address");
  await reference.fill("https://example.test/webhooks/telnyx");
  const save = async () => {
    const response = page.waitForResponse(
      (r) =>
        r.url().endsWith("/v1/admin/settings") &&
        r.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "Save Telnyx settings" }).click();
    expect((await response).ok()).toBeTruthy();
  };
  await save();
  await reference.fill("");
  await save();
  await page.reload();
  await openSettings(page);
  await openCard(page, "Calling");
  await expect(reference).toHaveValue("");
});

test("the AI card connects ChatGPT and the live help key in one place", async ({
  page,
}) => {
  await enter(page);
  await openSettings(page);
  const card = await openCard(page, "AI");
  await expect(card).toContainText("ChatGPT");
  await expect(card.getByText("Import Codex login")).toBeVisible();
  await expect(card.getByLabel("Import Codex login")).toBeAttached();
  await expect(
    card.getByLabel("Live help key (OpenRouter, Cerebras)", { exact: true }),
  ).toBeVisible();
  await expect(card.getByLabel("OpenAI API key", { exact: true })).toHaveCount(
    0,
  );
  // ChatGPT is the one route since R2.6; no API-key option is offered.
  await expect(
    card.getByText("Use an OpenAI API key instead of ChatGPT"),
  ).toHaveCount(0);
  for (const gone of ["Continue with ChatGPT", "Optional research search"])
    await expect(page.getByText(gone)).toHaveCount(0);
});

test("the script editor parses as you type and lists every problem with its line", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Script", exact: true }).click();
  const editor = page.getByLabel("Script Markdown");
  await expect(editor).toHaveValue(/# Opening/);
  await editor.fill("# Opening\nHello there\n# Pitch\nBuy now\n");
  const errors = page.locator(".script-errors");
  await expect(errors).toContainText("Line 1");
  await expect(errors).toContainText("Every Opening must say who is calling");
  await expect(errors).toContainText("Add a '# Close' section");
  await expect(errors).not.toContainText("Unknown section '# Pitch'");
  await expect(errors).not.toContainText("Add a '# Screening' section");
  await expect(
    page.getByRole("button", { name: "Save script" }),
  ).toBeDisabled();
  await editor.fill(
    "# Opening\nHi {first_name}, {rep} from {seller}. {{ why now, from research }}\n\n# Screening\n{rep} from {seller} about roofs.\n\n# Discovery\n- When was the roof checked?\n- Who handles repairs?\n\n# Offers\n## Inspection\nA free inspection.\n\n# Close\nWould Tuesday work?\n",
  );
  const outline = page.locator(".script-outline");
  await expect(outline).toContainText("2 options · AI picks up to 3 per lead");
  await expect(outline.locator(".ai-chip")).toContainText(
    "why now, from research",
  );
  const saved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/script/import") &&
      r.request().postDataJSON().preview === false,
  );
  await page.getByRole("button", { name: "Save script" }).click();
  expect((await saved).ok()).toBeTruthy();
  await expect(page.getByText(/Script saved/).first()).toBeVisible();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
});

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

async function enter(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible();
}

async function openSettings(page: Page) {
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByTestId("setting-Calling")).toBeVisible();
}

async function openCard(page: Page, title: string) {
  const card = page.getByTestId(`setting-${title}`);
  if (!(await card.evaluate((d) => (d as HTMLDetailsElement).open)))
    await card.locator(":scope > summary").click();
  return card;
}

async function openMore(page: Page, section: string) {
  const more = page.locator("details.more-settings");
  if (!(await more.evaluate((d) => (d as HTMLDetailsElement).open)))
    await more.locator(":scope > summary").click();
  const item = more
    .locator(".more-list > details")
    .filter({ has: page.locator(":scope > summary", { hasText: section }) });
  if (!(await item.evaluate((d) => (d as HTMLDetailsElement).open)))
    await item.locator(":scope > summary").click();
  return item;
}

/** Visible interactive controls in the page body: the first-screen budget. */
async function visibleControls(page: Page) {
  return await page
    .locator(
      ".page-body :is(button, input, select, textarea, summary, a[href])",
    )
    .evaluateAll(
      (nodes) =>
        // checkVisibility honours closed <details>, hidden pages and display:none.
        nodes.filter((node) =>
          (node as Element).checkVisibility({ visibilityProperty: true }),
        ).length,
    );
}

test("settings is one short page with everything else folded away", async ({
  page,
}) => {
  await enter(page);
  await openSettings(page);
  for (const title of [
    "Calling",
    "AI",
    "Script",
    "Leads",
    "Calling hours",
    "Do-not-call check",
  ])
    await expect(page.getByTestId(`setting-${title}`)).toBeVisible();
  await expect(page.locator(".tabs")).toHaveCount(0);
  await expect(page.getByLabel("Voice API connection ID")).toBeHidden();
  expect(await visibleControls(page)).toBeLessThanOrEqual(30);
  await page.getByRole("button", { name: "Check readiness" }).click();
  await expect(page.getByRole("region", { name: "Readiness" })).toBeVisible();
  for (const gone of [
    "Publication reason",
    "Business identity evidence",
    "Scheduled prepaid screening",
    "Capability evidence",
  ])
    await expect(page.getByText(gone)).toHaveCount(0);
  const more = await openMore(page, "AI budget");
  await expect(more.getByLabel("AI budget per lead list (USD)")).toBeVisible();
  for (const section of [
    "Campaign goal",
    "Do-not-call checks and consent",
    "Callbacks",
    "Users, backups and recovery",
  ])
    await expect(
      page.locator(".more-list > details > summary", { hasText: section }),
    ).toBeVisible();
});

test("calling policy publication is version-fenced and reloads after a conflict", async ({
  page,
}) => {
  await enter(page);
  await openSettings(page);
  const bodies: Record<string, unknown>[] = [];
  let conflict = true;
  await page.route("**/v1/admin/policies/*/publish", async (route) => {
    bodies.push(route.request().postDataJSON());
    if (conflict) {
      conflict = false;
      await route.fulfill({
        status: 409,
        json: {
          detail:
            "POLICY_VERSION_CONFLICT: a newer calling policy is current; reload it before publishing",
        },
      });
      return;
    }
    await route.continue();
  });
  await openCard(page, "Calling hours");
  await page.getByLabel("Attempts per lead").fill("3");
  await page
    .getByRole("button", { name: "Save calling hours", exact: true })
    .click();
  await expect(
    page.getByText(/Someone else changed this/).first(),
  ).toBeVisible();
  expect(bodies[0]).toMatchObject({
    max_attempts: 3,
    reason: "Edited in Settings",
    expected_version: expect.any(Number),
  });
  expect(bodies[0]).not.toHaveProperty("registry_required");
  await page.getByRole("button", { name: "Reload", exact: true }).click();
  await expect(page.getByLabel("Attempts per lead")).not.toHaveValue("3");
  await page.getByLabel("Attempts per lead").fill("3");
  const published = page.waitForResponse(
    (r) => r.url().endsWith("/publish") && r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save calling hours", exact: true })
    .click();
  expect((await published).ok()).toBeTruthy();
  await expect(page.getByText("Saved.").first()).toBeVisible();
  await expect(page.getByLabel("Attempts per lead")).toHaveValue("3");
  // Restore the shared fixture policy for later journeys.
  await page.getByLabel("Attempts per lead").fill("4");
  const restored = page.waitForResponse(
    (r) => r.url().endsWith("/publish") && r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save calling hours", exact: true })
    .click();
  expect((await restored).ok()).toBeTruthy();
});

test("form bounds come from the authority's definitions", async ({ page }) => {
  await page.route("**/v1/definitions", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.bounds.policy.max_attempts.max = 7;
    await route.fulfill({ response, json: body });
  });
  await enter(page);
  await openSettings(page);
  await openCard(page, "Calling hours");
  await expect(page.getByLabel("Attempts per lead")).toHaveAttribute(
    "max",
    "7",
  );
});

test("advanced detail is one collapsed view and debug controls are gone", async ({
  page,
}) => {
  const strategyReads: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/v1/optimization/strategies"))
      strategyReads.push(r.url());
  });
  await enter(page);
  await page.getByRole("button", { name: "Script", exact: true }).click();
  for (const gone of [
    "Process queued work",
    "Publish approved variant",
    "Reconcile records",
    "Evaluate reviewed replay",
  ])
    await expect(page.getByRole("button", { name: gone })).toHaveCount(0);
  await expect(page.getByLabel("Conversion goal")).toHaveCount(0);
  await openSettings(page);
  expect(strategyReads).toEqual([]);
  await openMore(page, "Learning");
  await expect(page.getByLabel("Human pickup prior successes")).toHaveCount(0);
  await page.getByText("Sparse estimate assumptions", { exact: true }).click();
  await expect(page.getByTestId("estimate-assumptions")).toContainText(
    "Human pickup prior",
  );
  for (const gone of [
    "Reviewed replay cases JSON",
    "External reputation evidence",
    "Carrier record JSON",
  ])
    await expect(page.getByLabel(gone)).toHaveCount(0);
  await expect(
    page.getByRole("table", { name: "Rolling evaluation support" }),
  ).toHaveCount(0);
});

test("an imported list is researched and scripted before it can be called", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  const list = `Prep list ${Date.now()}`;
  await page.getByLabel("List name").fill(list);
  // Fictional 555-01xx range: a clock-derived exchange can start with 0 and be invalid. Fresno's
  // area code keeps it clear of the fixed 415 fixtures other journeys import.
  const phone = `+155955501${String(Date.now()).slice(-2)}`;
  await page
    .getByLabel("CSV lead data")
    .fill(
      `name,phone,company,city\nDana Reyes,${phone},Hilltop Apartments,Fresno`,
    );
  const imported = page.waitForResponse((r) =>
    r.url().endsWith("/v1/leads/import"),
  );
  await page.getByRole("button", { name: "Import leads" }).click();
  const body = (await imported).request().postDataJSON();
  expect(body.prepare).toBe(true);
  await expect(page.getByText(/Agents are researching them now/)).toBeVisible();
  const panel = page.getByRole("region", { name: "Lead preparation" });
  await expect(panel.getByText(list)).toBeVisible();
  await expect(panel.getByText(/1 of 1 ready to call/)).toBeVisible({
    timeout: 30000,
  });
  await page.getByLabel("Search leads").fill("Dana Reyes");
  await page.getByRole("button", { name: "Search lead history" }).click();
  await page
    .getByRole("button", { name: "Open lead Dana Reyes" })
    .first()
    .click();
  const packet = page.getByRole("dialog", { name: "Lead packet" });
  await expect(packet.getByText("Script for this lead")).toBeVisible();
  await expect(packet.locator(".ai-text").first()).toBeVisible();
  await expect(packet.getByText("What the agent found")).toBeVisible();
});

test("disabled actions say why and settings failures stay recoverable", async ({
  page,
}) => {
  let failSettings = true;
  await page.route("**/v1/admin/settings", async (route) => {
    if (failSettings && route.request().method() === "GET") {
      await route.fulfill({
        status: 503,
        json: { detail: "Settings temporarily unavailable" },
      });
      return;
    }
    await route.continue();
  });
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.quiescence = { can_disconnect: false, reasons: ["active_call"] };
    await route.fulfill({ response, json: body });
  });
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByText(/Settings could not be loaded/)).toBeVisible();
  failSettings = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("setting-Calling")).toBeVisible();
  await openMore(page, "Workspace and app updates");
  await expect(page.getByText("Finish first: Active call.")).toBeVisible();
  await openScreening(page);
});

test("managed setup import shows connection and hides tunnel fields", async ({
  page,
}) => {
  await page.addInitScript(() =>
    localStorage.setItem(
      "opendialer.session",
      JSON.stringify({ connection: "local", simulation: false }),
    ),
  );
  let configured = false;
  await page.route("**/v1/managed-connection", (route) =>
    route.fulfill({
      json: {
        configured,
        connected: configured,
        state: configured ? "connected" : "direct",
        label: "Test caller",
        numbers: 0,
      },
    }),
  );
  await page.route("**/v1/admin/managed-connection", async (route) => {
    expect(route.request().postDataJSON().label).toBe("Test caller");
    configured = true;
    await route.fulfill({
      json: {
        configured: true,
        connected: true,
        state: "connected",
        label: "Test caller",
        numbers: 0,
      },
    });
  });
  // A returning, non-simulated local session: the restore path shows the setup-file import.
  await setUpAuthority(page);
  await enter(page);
  await openSettings(page);
  await openCard(page, "Calling");
  await page.getByLabel("Import calling setup (.json)").setInputFiles({
    name: "caller.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify({ label: "Test caller" })),
  });
  await expect(
    page.getByRole("status").filter({ hasText: "Test caller" }),
  ).toContainText("Connected");
  await expect(page.getByLabel("Webhook address")).toHaveCount(0);
  await expect(page.getByLabel("Voice API connection ID")).toHaveCount(0);
  await expect(
    page.getByLabel("Telnyx API key", { exact: true }),
  ).toBeVisible();
});
