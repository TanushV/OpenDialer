import { expect, test } from "@playwright/test";
import { prepareSimulationCall } from "./simulation";

for (const connected of [false, true]) {
  test(`follow-up lost-response retry retains identity and snooze preserves the promise ${connected ? "postgres" : "local"}`, async ({
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
    if (connected) {
      await page.getByTitle("Sign out").click();
      await page.getByRole("button", { name: "Connect to a server" }).click();
      await page.getByLabel("Server URL").fill("http://127.0.0.1:8766");
      await page.getByLabel("Email").fill("team@example.test");
      await page
        .getByLabel("Password", { exact: true })
        .fill("Connected-test-password-123");
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
    }
    await prepareSimulationCall(
      page,
      "Follow-up retry fixture",
      "+14155550177",
    );
    await page
      .getByRole("button", { name: "Get next lead", exact: true })
      .click();
    await page.getByRole("button", { name: "Start call", exact: true }).click();
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    await page.getByLabel("Disposition").selectOption("callback_requested");
    const saved = page.waitForResponse(
      (r) =>
        r.url().endsWith("/disposition") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Save outcome" }).click();
    const call = await (await saved).json();
    await page
      .getByRole("button", { name: "Intelligence", exact: true })
      .click();
    const task = page.locator(`[data-call-id="${call.id}"]`);
    await task
      .getByLabel("Callback time (your device timezone)")
      .fill("2026-10-22T13:30");
    const requests: Record<string, unknown>[] = [];
    await page.route("**/v1/tasks/*", async (route) => {
      if (route.request().method() !== "PATCH") return route.continue();
      requests.push(route.request().postDataJSON());
      const response = await route.fetch();
      if (requests.length === 1) await route.abort("failed");
      else await route.fulfill({ response });
    });
    await task.getByRole("button", { name: "Save follow-up" }).click();
    await expect(task.getByText("Open", { exact: true })).toBeVisible();
    expect(requests.length).toBe(2);
    expect(requests[0].request_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(requests[1]).toEqual(requests[0]);
    await page.unroute("**/v1/tasks/*");
    await task
      .getByLabel("Reminder until (your device timezone)")
      .fill("2026-10-21T11:00");
    const snoozed = page.waitForResponse(
      (r) => r.url().includes("/v1/tasks/") && r.request().method() === "PATCH",
    );
    await task
      .getByRole("button", { name: "Snooze reminder", exact: true })
      .click();
    const result = await (await snoozed).json();
    expect(Date.parse(result.due_at)).toBe(Date.parse(String(requests[0].due_at)));
    expect(result.snoozed_until).not.toBe(result.due_at);
    await page.reload();
    await page
      .getByRole("button", { name: "Intelligence", exact: true })
      .click();
    await expect(
      task.getByLabel("Callback time (your device timezone)"),
    ).toHaveValue("2026-10-22T13:30");
    await expect(
      task.getByLabel("Reminder until (your device timezone)"),
    ).toHaveValue("2026-10-21T11:00");
    await task.getByRole("button", { name: "Complete", exact: true }).click();
    await expect(task.getByText("Completed", { exact: true })).toBeVisible();
  });
}

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
