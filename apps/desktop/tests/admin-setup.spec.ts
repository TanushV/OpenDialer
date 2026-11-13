import { AUTHORITY, expect, test, type Page, setUpAuthority } from "./fixtures";
import { openScreening, clickUntil } from "./navigation";
import { answer, hangUp, saveOutcome } from "./simulation";

test("the Telnyx key is saved and checked in its own row", async ({ page }) => {
  await enter(page);
  await openSettings(page);
  const row = page.getByTestId("setup-telnyx");
  await row.getByLabel("New Telnyx API key").fill("synthetic-onboarding-key");
  const saved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/settings") && r.request().method() === "PUT",
  );
  await row.getByRole("button", { name: "Save and check" }).click();
  const response = await saved;
  expect(response.ok()).toBeTruthy();
  expect(response.request().postDataJSON()).toMatchObject({
    telnyx_api_key: "synthetic-onboarding-key",
  });
  await expect(row.getByRole("status")).toContainText("Checked");
  await expect(row.getByLabel("New Telnyx API key")).toHaveValue("");
  // The connection fields come with the setup file and are never typed here (section 7).
  for (const gone of ["Voice API connection ID", "Webhook address"])
    await expect(page.getByLabel(gone)).toHaveCount(0);
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

test("the ChatGPT row holds its login and the OpenRouter key has its own row", async ({
  page,
}) => {
  await enter(page);
  await openSettings(page);
  const chatgpt = page.getByTestId("setup-chatgpt");
  await expect(chatgpt).toContainText("ChatGPT");
  await expect(chatgpt.getByText("Open Codex auth.json")).toBeVisible();
  await expect(chatgpt.getByLabel("Open Codex auth.json")).toBeAttached();
  await expect(chatgpt.getByLabel("Research uses")).toBeVisible();
  await expect(
    page.getByTestId("setup-openrouter").getByLabel("New OpenRouter key"),
  ).toBeVisible();
  // ChatGPT is the one research route since R2.6; no API-key option is offered.
  await expect(page.getByLabel("OpenAI API key", { exact: true })).toHaveCount(
    0,
  );
  for (const gone of [
    "Use an OpenAI API key instead of ChatGPT",
    "Continue with ChatGPT",
    "Optional research search",
  ])
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
  await expect(page.getByTestId("setup-telnyx")).toBeVisible();
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
  // R7.2: rows only; each row holds its own control, and nothing but Advanced is folded.
  await expect(page.locator("[data-testid^='setting-']")).toHaveCount(0);
  for (const id of [
    "telnyx",
    "connection",
    "chatgpt",
    "openrouter",
    "test_phone",
    "hours",
  ])
    await expect(page.getByTestId(`setup-${id}`)).toBeVisible();
  await expect(
    page
      .getByTestId("setup-hours")
      .getByRole("button", { name: "Save calling hours" }),
  ).toBeVisible();
  await expect(page.locator(".tabs")).toHaveCount(0);
  await expect(page.getByLabel("Voice API connection ID")).toBeHidden();
  expect(await visibleControls(page)).toBeLessThanOrEqual(30);
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
    "Needs attention",
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
  await page.getByTestId("setup-hours").waitFor();
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
  await page.getByTestId("setup-hours").waitFor();
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
  await openMore(page, "Number limits");
  // D31: StatML is archived, with its estimates and training switches.
  for (const gone of [
    "Human pickup prior successes",
    "Exploration share",
    "Minimum training rows",
    "Automatic training from call history",
  ])
    await expect(page.getByLabel(gone)).toHaveCount(0);
  await expect(page.getByTestId("estimate-assumptions")).toHaveCount(0);
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

test("an imported list waits for the rep to start script generation", async ({
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
  expect(body.prepare ?? false).toBe(false);
  const panel = page.getByRole("region", { name: "Lead preparation" });
  const preparation = panel.getByRole("listitem").filter({ hasText: list });
  await expect(preparation).toContainText("1 awaiting generation");
  await preparation.getByRole("button", { name: "Generate scripts" }).click();
  await expect(preparation.getByText(/1 of 1 processed/)).toBeVisible({
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

test("a slow failed settings read never brings the error back after Try again worked", async ({
  page,
}) => {
  // Reads fail at first; the read from the first Try again fails late, after the second Try again worked.
  let phase: "failing" | "hold" | "working" = "failing";
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  let holding = false;
  await page.route("**/v1/admin/settings", async (route) => {
    if (route.request().method() !== "GET" || phase === "working")
      return route.continue();
    if (phase === "hold" && !holding) {
      holding = true;
      await held;
    }
    return route.fulfill({ status: 503, json: { detail: "Unavailable" } });
  });
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const failed = page.getByText(/Settings could not be loaded/);
  await expect(failed).toBeVisible();
  phase = "hold";
  await page.getByRole("button", { name: "Try again" }).click();
  await expect.poll(() => holding).toBe(true);
  phase = "working";
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(failed).toHaveCount(0);
  release();
  await page.waitForTimeout(500);
  await expect(failed).toHaveCount(0);
});

test("settings failures stay recoverable", async ({ page }) => {
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
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByText(/Settings could not be loaded/)).toBeVisible();
  failSettings = false;
  await clickUntil(page.getByRole("button", { name: "Try again" }), () =>
    expect(page.getByText(/Settings could not be loaded/)).toHaveCount(0, {
      timeout: 3_000,
    }),
  );
  await openMore(page, "App updates");
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
  await expect(page.getByText("<rep>-calling-setup.json")).toBeVisible();
  const chooser = page.waitForEvent("filechooser");
  await page
    .getByRole("button", { name: "Open calling setup (.json)" })
    .click();
  await (
    await chooser
  ).setFiles({
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
    page.getByTestId("setup-telnyx").getByLabel("New Telnyx API key"),
  ).toBeVisible();
});

test("a settings row turns green only after its check and says what it powers", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const row = page.getByTestId("setup-openrouter");
  await expect(row).toContainText("Live helper, summaries, fallback agent");
  const status = row.getByRole("status", { name: "OpenRouter key status" });
  await expect(status).not.toContainText("Checked");
  await row.getByLabel("New OpenRouter key").fill("sk-or-test-only");
  await row.getByRole("button", { name: "Save and check" }).click();
  await expect(status).toContainText("Checked");
  await expect(page.getByRole("region", { name: "Readiness" })).toBeVisible();
});

test("Test call limits calling to the test phone, and the session ends with that call", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const row = page.getByTestId("setup-test_phone");
  const phone = row.getByLabel("New Test phone");
  // Focus the offscreen field with a click before typing. WebKit's fill can
  // leave it empty when it is below the scrollport.
  await phone.click();
  await phone.pressSequentially("+1 202 555 0199");
  await expect(phone).toHaveValue("+1 202 555 0199");
  await row.getByRole("button", { name: "Save and check" }).click();
  await row.getByRole("button", { name: "Test call", exact: true }).click();
  const placed = page.waitForResponse(
    (r) => r.url().endsWith("/v1/calls/start") && r.ok(),
  );
  await page.getByRole("button", { name: /^Start calling/ }).click();
  await expect(page.getByTestId("lead-name")).toHaveText("Your test phone");
  // Pause once the call is placed: a Pause before that rightly places no call.
  await placed;
  await page.getByRole("button", { name: /^Pause/ }).click();
  await page.getByRole("button", { name: /^Skip/ }).click();
  await expect(
    page.getByRole("button", { name: /^Start calling/ }),
  ).toBeVisible();
  // A test call is one call: once it is settled the session has ended by itself.
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByTestId("setup-test_phone")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "End test session", exact: true }),
  ).toBeHidden();
});

test("a slow re-read of the rows never undoes a check that finished after it", async ({
  page,
}) => {
  await enter(page);
  const local = await page.request.post(`${AUTHORITY}/v1/local/session`, {
    headers: { "X-Local-Token": "e2e-local-token" },
    data: {},
  });
  const token = (await local.json()).access_token;
  await page.request.put(`${AUTHORITY}/v1/admin/settings`, {
    headers: { authorization: `Bearer ${token}` },
    data: { openrouter_api_key: "sk-or-saved-earlier" },
  });
  // A second read of the rows answers late, so it lands after the rep's check.
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  let reads = 0;
  await page.route("**/v1/settings/rows", async (route) => {
    reads += 1;
    if (reads === 1) return route.continue();
    // The server answers now (not checked yet); the answer reaches the page only after the check.
    const response = await route.fetch();
    await held;
    await route.fulfill({ response });
  });
  await openSettings(page);
  const row = page.getByTestId("setup-openrouter");
  const status = row.getByRole("status", { name: "OpenRouter key status" });
  await expect(status).toContainText("Not checked");
  await expect.poll(() => reads).toBeGreaterThan(1);
  await row.getByRole("button", { name: "Check", exact: true }).click();
  await expect(status).toContainText("Checked:");
  release();
  await page.waitForLoadState("networkidle");
  await expect(status).toContainText("Checked:");
  await expect(status).not.toContainText("Not checked");
});

test("a key saved without a check can be checked in its row without typing it again", async ({
  page,
}) => {
  await enter(page);
  // A key saved before the rows existed (or by an older version) has no check yet.
  const local = await page.request.post(`${AUTHORITY}/v1/local/session`, {
    headers: { "X-Local-Token": "e2e-local-token" },
    data: {},
  });
  const token = (await local.json()).access_token;
  const saved = await page.request.put(`${AUTHORITY}/v1/admin/settings`, {
    headers: { authorization: `Bearer ${token}` },
    data: { openrouter_api_key: "sk-or-saved-earlier" },
  });
  expect(saved.ok()).toBeTruthy();
  await openSettings(page);
  const row = page.getByTestId("setup-openrouter");
  const status = row.getByRole("status", { name: "OpenRouter key status" });
  await expect(status).toContainText("Not checked");
  await row.getByRole("button", { name: "Check", exact: true }).click();
  await expect(status).toContainText("Checked");
  await expect(row.getByLabel("New OpenRouter key")).toHaveValue("");
});

test("a saved test call pauses calling instead of counting down to a prospect", async ({
  page,
}) => {
  // Live check, November 10: after the test call's outcome was saved, the Dialer counted down to the next lead
  // from the rep's real lists.
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const row = page.getByTestId("setup-test_phone");
  await row.getByLabel("New Test phone").fill("+1 202 555 0188");
  await row.getByRole("button", { name: "Save and check" }).click();
  await row.getByRole("button", { name: "Test call", exact: true }).click();
  await page.getByRole("button", { name: /^Start calling/ }).click();
  await expect(page.getByTestId("lead-name")).toHaveText("Your test phone");
  await answer(page);
  await hangUp(page).click();
  let starts = 0;
  page.on("request", (r) => {
    if (r.url().endsWith("/v1/calls/start")) starts++;
  });
  await saveOutcome(page, "not_interested");
  await expect(
    page.getByRole("button", { name: /^Start calling/ }),
  ).toBeVisible();
  await expect(page.getByText(/Next call in/)).toHaveCount(0);
  await page.waitForTimeout(4_000);
  expect(starts).toBe(0);
});
