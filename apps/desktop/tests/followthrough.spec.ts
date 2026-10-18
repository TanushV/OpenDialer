import { expect, test } from "@playwright/test";

test("inferred follow-ups show time windows and source correction conflicts", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const workspace = await response.json();
    workspace.tasks = [
      {
        id: "inferred-task",
        version: 2,
        call_id: "source-call",
        lead_id: workspace.leads[0].id,
        owner_id: workspace.user.id,
        kind: "callback",
        action: "Confirm the next conversation",
        due_at: null,
        timezone: "America/Los_Angeles",
        status: "needs_confirmation",
        time_window: {
          date: "2026-10-20",
          period: "afternoon",
          timezone: "America/Los_Angeles",
        },
        source_stale: true,
        review_required: true,
      },
    ];
    await route.fulfill({ response, json: workspace });
  });
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  )
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  const task = page.locator('[data-call-id="source-call"]');
  await expect(
    task.getByText(
      "Requested window: 2026-10-20 · afternoon · America/Los_Angeles",
    ),
  ).toBeVisible();
  await expect(
    task.getByText(
      "Source corrected. Confirm this follow-up; your edits are preserved.",
    ),
  ).toBeVisible();
  await expect(
    task.getByRole("button", { name: "Save follow-up" }),
  ).toBeDisabled();
});

test("a local callback is confirmed, rescheduled, retained on reload and completed", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  )
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  if (await page.getByRole("button", { name: "Pause dialing" }).isVisible())
    await page.getByRole("button", { name: "Pause dialing" }).click();
  await page.getByRole("button", { name: "Go ready" }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await page.getByLabel("Disposition").selectOption("callback_requested");
  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/disposition") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Save outcome" }).click();
  const call = await (await saved).json();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  const task = page.locator(`[data-call-id="${call.id}"]`);
  await expect(
    task.getByText("Needs confirmation", { exact: true }),
  ).toBeVisible();
  await task
    .getByLabel("Callback time (your device timezone)")
    .fill("2026-10-21T11:00");
  await task.getByRole("button", { name: "Save follow-up" }).click();
  await expect(task.getByText("Open", { exact: true })).toBeVisible();
  await task
    .getByLabel("Callback time (your device timezone)")
    .fill("2026-10-22T13:30");
  await task.getByRole("button", { name: "Save follow-up" }).click();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(
    task.getByLabel("Callback time (your device timezone)"),
  ).toHaveValue("2026-10-22T13:30");
  await task.getByRole("button", { name: "Complete", exact: true }).click();
  await expect(task.getByText("Completed", { exact: true })).toBeVisible();
  await expect(
    task.getByRole("button", { name: "Complete", exact: true }),
  ).toBeDisabled();
});

test("unfinished wrap-up survives navigation and a full reload", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  if (
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .isVisible()
  )
    await page
      .getByRole("button", { name: "Try a simulated workspace" })
      .click();
  // This journey owns one additional fictional lead; other flows share the authority.
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("CSV lead data")
    .fill(
      "name,phone,company,timezone,region\nRestart Fixture,+14155550119,Example,America/Los_Angeles,415",
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/1 imported/)).toBeVisible();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  if (await page.getByRole("button", { name: "Go ready" }).isVisible())
    await page.getByRole("button", { name: "Go ready" }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await expect(page.getByLabel("Disposition")).toBeVisible();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await expect(page.getByLabel("Disposition")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Disposition")).toBeVisible();
  await page.getByLabel("Disposition").selectOption("no_answer");
  const sent = page.waitForRequest(
    (r) => r.url().endsWith("/disposition") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "Save outcome" }).click();
  expect((await sent).postDataJSON().expected_version).toBeGreaterThan(0);
  await expect(
    page.getByText("Outcome saved. Analysis continues in the background."),
  ).toBeVisible();
});
