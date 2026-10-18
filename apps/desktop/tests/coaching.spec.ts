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
