import { expect, test, type Page } from "@playwright/test";

async function enter(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
}

test("personal practice goals survive reload and practice requires no call", async ({
  page,
}) => {
  await enter(page);
  const panel = page.getByRole("region", { name: "Coaching and practice" });
  await panel
    .getByRole("textbox", { name: "Practice goal" })
    .fill("Clarify ownership before pitching");
  await panel
    .getByRole("button", { name: "Save practice goal", exact: true })
    .click();
  await expect(
    panel.getByText("Clarify ownership before pitching", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await expect(
    panel.getByText("Clarify ownership before pitching", { exact: true }),
  ).toBeVisible();
  await panel
    .getByRole("textbox", { name: "Practice scenario" })
    .fill("How can I clarify a new objection?");
  await panel
    .getByRole("button", { name: "Practice privately", exact: true })
    .click();
  await expect(
    panel.getByText(
      "AI practice is unavailable. Your approved knowledge remains available.",
    ),
  ).toBeVisible();
  await expect(
    panel.getByText(
      "Practice never places a call or records a business outcome.",
    ),
  ).toBeVisible();
  await panel
    .getByRole("button", { name: "Complete goal", exact: true })
    .click();
  await expect(panel.getByText("Completed", { exact: true })).toBeVisible();
});

test("private practice text expires and a late abandoned response cannot reappear", async ({
  page,
}) => {
  await page.clock.install();
  let calls = 0;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/coaching/practice", async (route) => {
    calls++;
    if (calls === 2) await pending;
    await route.fulfill({
      json: {
        simulation: true,
        status: "ready",
        raw_remaining_ms: 1000,
        guidance: {
          action: "SHOW_CARD",
          say_or_ask: "Which team owns the decision?",
          reason: "Ask before assuming.",
        },
      },
    });
  });
  await enter(page);
  const panel = page.getByRole("region", { name: "Coaching and practice" });
  try {
    await panel
      .getByRole("textbox", { name: "Practice scenario" })
      .fill("An unfamiliar decision process");
    await panel
      .getByRole("button", { name: "Practice privately", exact: true })
      .click();
    await expect(
      panel.getByText("Which team owns the decision?", { exact: true }),
    ).toBeVisible();
    await page.clock.runFor(1100);
    await expect(
      panel.getByText("Which team owns the decision?", { exact: true }),
    ).toBeHidden();
    await panel
      .getByRole("textbox", { name: "Practice scenario" })
      .fill("Another private scenario");
    await panel
      .getByRole("button", { name: "Practice privately", exact: true })
      .click();
    await expect(
      panel.getByRole("button", { name: "Discard practice", exact: true }),
    ).toBeVisible();
    await panel
      .getByRole("button", { name: "Discard practice", exact: true })
      .click();
    release();
    await expect(
      panel.getByRole("textbox", { name: "Practice scenario" }),
    ).toHaveValue("");
    await expect(
      panel.getByText("Which team owns the decision?", { exact: true }),
    ).toBeHidden();
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("a reviewed coaching tip can be published and withdrawn with its source version", async ({
  page,
}) => {
  let published = false;
  const example = {
    id: "reviewed-example",
    version: 1,
    text: "Clarify ownership first.",
    raw_evidence_status: "expired",
  };
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.call.transcript_version = 7;
    body.call_memory = {
      coaching_points: [
        { id: "tip-one", text: example.text, status: "confirmed" },
      ],
    };
    await route.fulfill({ response, json: body });
  });
  await page.route("**/v1/coaching", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: {
        ...(await response.json()),
        examples: published ? [example] : [],
      },
    });
  });
  await page.route("**/v1/admin/snippets", async (route) => {
    const payload = route.request().postDataJSON();
    expect(payload.coaching_point_id).toBe("tip-one");
    expect(payload.expected_transcript_version).toBe(7);
    expect(payload.review_reference).toBe("Reviewed procurement clarification");
    expect(payload.text).toBeUndefined();
    published = true;
    await route.fulfill({ json: example });
  });
  await page.route("**/v1/admin/snippets/reviewed-example", async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      expected_version: 1,
      approved: false,
    });
    published = false;
    await route.fulfill({ json: { ...example, approved: false, version: 2 } });
  });
  await enter(page);
  // Create a real simulation call so this UI contract does not depend on earlier tests.
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await page.getByRole("button", { name: "Go ready", exact: true }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await page.getByLabel("Disposition").selectOption("no_answer");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: /^Review call / })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Review for team", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Example review reference", exact: true })
    .fill("Reviewed procurement clarification");
  await page
    .getByRole("button", { name: "Publish reviewed example", exact: true })
    .click();
  await expect(
    page.getByText("Published as a reviewed example", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "All conversations", exact: true })
    .click();
  await page.getByText("Reviewed examples", { exact: true }).click();
  const panel = page.getByRole("region", { name: "Coaching and practice" });
  await expect(panel.getByText(example.text, { exact: true })).toBeVisible();
  await panel
    .getByRole("button", { name: "Withdraw example", exact: true })
    .click();
  await expect(panel.getByText(example.text, { exact: true })).toBeHidden();
});
