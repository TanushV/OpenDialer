import { expect, test, type Page } from "./fixtures";
import { importLead, chooseOutcome } from "./simulation";

async function enter(page: Page, connected = false) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
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
  test(`fixed proactive coaching, script, statistical brief and local wrapup (${connected ? "connected" : "local"})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1028, height: 749 });
    await enter(page, connected);
    await importLead(
      page,
      `Proactive coaching fixture ${connected ? "connected" : "local"}`,
      connected ? "+14155550708" : "+14155550707",
    );
    if (await page.getByRole("button", { name: "Pause dialing" }).isVisible())
      await page.getByRole("button", { name: "Pause dialing" }).click();
    await page.getByRole("button", { name: "Go ready" }).click();
    await page.getByRole("button", { name: "Get next lead" }).click();
    await expect(page.getByTestId("prospect-brief")).toBeVisible();
    await expect(page.getByTestId("statistical-guidance")).toBeVisible();
    await expect(page.getByTestId("prepared-script")).toBeVisible();
    await page.getByRole("button", { name: "Start call", exact: true }).click();
    await page.getByRole("button", { name: "Simulate human answer" }).click();
    await page
      .getByLabel("Simulation transcript")
      .fill("Our approvals depend on a union committee.");
    await page.getByRole("button", { name: "Add prospect turn" }).click();
    const strip = page.getByTestId("live-coaching-strip");
    await expect(strip).toBeVisible();
    await page.mouse.wheel(0, 1200);
    const bounds = await strip.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThan(400);
    await expect(
      page.getByRole("button", { name: "Hang up", exact: true }),
    ).toBeInViewport();
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    await chooseOutcome(page, "meeting_booked");
    await page.getByRole("button", { name: "Save outcome" }).click();
    await expect(page.getByText(/Meeting recorded.*rep/i)).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: /create calendar|send invitation|sync CRM/i,
      }),
    ).toHaveCount(0);
  });
}

test("no-subscription costs, short retention and prepaid DNC configuration are explicit", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Policy & DNC", exact: true }).click();
  await expect(
    page.getByText("Raw calls and transcripts: five minutes at most.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText(/No subscriptions/i).first()).toBeVisible();
  await expect(page.getByLabel("Prepaid DNC API key")).toBeVisible();
  await expect(page.getByText(/seller.*authorization/i).first()).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: /enable reputation subscription|connect calendar/i,
    }),
  ).toHaveCount(0);
});

test("batch script preparation exposes gatekeeper and screening sections", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await expect(page.getByTestId("preparation-runway")).toBeVisible();
  await page
    .getByRole("button", { name: "Prepare displayed lead packets" })
    .click();
  // The count and budget preview precedes queueing.
  await page.getByRole("button", { name: "Queue preparation" }).click();
  await expect(page.getByText(/Preparation queued/)).toBeVisible();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  const ready = page.getByRole("button", { name: "Go ready", exact: true });
  if (await ready.isVisible()) await ready.click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  const script = page.getByTestId("prepared-script");
  await expect(
    script.getByRole("heading", { name: "Gatekeeper", exact: true }),
  ).toBeVisible();
  await expect(
    script.getByRole("heading", { name: "Screening", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
});

test("seller can save a fixed script and an overnight preparation window", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await page
    .getByLabel("Script topic", { exact: true })
    .fill("service scheduling");
  await page
    .getByLabel("Gatekeeper template", { exact: true })
    .fill("Who handles {topic} for your team?");
  await page.getByLabel("Prepared-ahead target", { exact: true }).fill("6");
  await expect(
    page.getByLabel("Final-attempt voicemail reference", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Workspace preparation cap", { exact: true })
    .fill("30");
  await page
    .getByLabel("Preparation starts at hour", { exact: true })
    .fill("18");
  await page.getByLabel("Preparation ends at hour", { exact: true }).fill("8");
  await page
    .getByRole("button", { name: "Save script and preparation" })
    .click();
  await expect(page.getByText("Script and preparation saved")).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await expect(page.getByLabel("Script topic", { exact: true })).toHaveValue(
    "service scheduling",
  );
  await expect(
    page.getByLabel("Workspace preparation cap", { exact: true }),
  ).toHaveValue("30");
  await expect(
    page.getByLabel("Preparation starts at hour", { exact: true }),
  ).toHaveValue("18");
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await expect(page.getByTestId("preparation-runway")).toContainText(
    "Target 6",
  );
  await expect(page.getByTestId("preparation-runway")).toContainText(
    "Outside preparation window",
  );
  await expect(page.getByTestId("preparation-runway")).toContainText(
    "declared schedule",
  );
  await expect(page.getByTestId("preparation-runway")).toContainText(
    "inspected",
  );
});

test("a provider choice made before settings finish loading is the one saved", async ({
  page,
}) => {
  await enter(page);
  // Hold every settings read until after the choice, as a slow authority would.
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/v1/admin/settings", async (route) => {
    if (route.request().method() === "GET") await held;
    await route.continue();
  });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Advanced", { exact: true }).click();
  const search = page.getByLabel("Optional research search", { exact: true });
  await search.selectOption("openai_web_search");
  const loaded = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/admin/settings") && r.request().method() === "GET",
  );
  release();
  await loaded;
  await expect(search).toHaveValue("openai_web_search");
  const saved = page.waitForRequest(
    (r) => r.url().endsWith("/v1/admin/settings") && r.method() === "PUT",
  );
  await page
    .getByRole("button", { name: "Save provider settings", exact: true })
    .click();
  expect((await saved).postDataJSON().research_search).toBe(
    "openai_web_search",
  );
  await expect(
    page.getByText("Provider configuration saved securely."),
  ).toBeVisible();
  await search.selectOption("disabled");
  await page
    .getByRole("button", { name: "Save provider settings", exact: true })
    .click();
  await expect(
    page.getByText("Provider configuration saved securely."),
  ).toBeVisible();
});

test("optional metered research search is an explicit saved choice", async ({
  page,
}) => {
  await enter(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Advanced", { exact: true }).click();
  const search = page.getByLabel("Optional research search", { exact: true });
  await expect(search).toHaveValue("disabled");
  await search.selectOption("openai_web_search");
  await page
    .getByRole("button", { name: "Save provider settings", exact: true })
    .click();
  await expect(
    page.getByText("Provider configuration saved securely."),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByText("Advanced", { exact: true }).click();
  await expect(search).toHaveValue("openai_web_search");
  await expect(
    page.getByText(/At most two metered searches per company/),
  ).toBeVisible();
  await search.selectOption("disabled");
  await page
    .getByRole("button", { name: "Save provider settings", exact: true })
    .click();
});
