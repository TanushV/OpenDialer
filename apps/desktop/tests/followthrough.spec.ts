import { expect, test } from "./fixtures";
import {
  importLead,
  prepareSimulationCall,
  chooseOutcome,
  waitForPreparation,
} from "./simulation";

for (const connected of [false, true]) {
  test(`correct a confirmed meeting without losing the rep note, then review outreach ${connected ? "postgres" : "local"}`, async ({
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
      "Meeting correction fixture",
      "+14155550991",
    );
    const packetResponse = page.waitForResponse((r) =>
      /\/v1\/leads\/[^/]+\/packet$/.test(r.url()),
    );
    await page
      .getByRole("button", { name: "Get next lead", exact: true })
      .click();
    const packet = await (await packetResponse).json();
    await page.getByRole("button", { name: "Start call", exact: true }).click();
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    await chooseOutcome(page, "meeting_held");
    await page
      .getByLabel("Who did you speak with?")
      .selectOption("right_party");
    await page
      .getByLabel("Wrap-up note")
      .fill("Keep my original human note through the correction.");
    await page
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    await expect(page.getByText(/Meeting recorded.*rep/i)).toBeVisible();
    await page
      .getByRole("button", { name: "Pause dialing", exact: true })
      .click();
    await page.getByRole("button", { name: "Review", exact: true }).click();
    await page
      .getByRole("button", {
        name: `Review call ${packet.lead.name}`,
        exact: true,
      })
      .first()
      .click();
    await page.getByText("Correct confirmed outcome", { exact: true }).click();
    await expect(page.getByLabel("Disposition")).toHaveValue("meeting_held");
    await expect(page.getByLabel("Who did you speak with?")).toHaveValue(
      "right_party",
    );
    await expect(page.getByLabel("Wrap-up note")).toHaveValue(
      "Keep my original human note through the correction.",
    );
    await chooseOutcome(page, "right_party");
    const correctionBodies: unknown[] = [];
    await page.route("**/v1/calls/*/disposition", async (route) => {
      correctionBodies.push(route.request().postDataJSON());
      const response = await route.fetch();
      expect(response.ok()).toBeTruthy();
      if (correctionBodies.length === 1) await route.abort("failed");
      else await route.fulfill({ response });
    });
    const saved = page.waitForResponse(
      (r) =>
        r.url().endsWith("/disposition") && r.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    expect((await saved).ok()).toBeTruthy();
    expect(correctionBodies).toHaveLength(2);
    expect(correctionBodies[1]).toEqual(correctionBodies[0]);
    await page.unroute("**/v1/calls/*/disposition");
    await page.getByRole("button", { name: "Leads", exact: true }).click();
    await page
      .getByRole("button", {
        name: `Open lead ${packet.lead.name}`,
        exact: true,
      })
      .click();
    const modal = page.locator(".modal");
    await modal
      .getByText("Prior conversation & next step", { exact: true })
      .click();
    await expect(
      modal
        .getByRole("status")
        .filter({ hasText: /^Outreach paused: meeting next action$/i }),
    ).toBeVisible();
    await modal.getByText("Record a meeting outcome", { exact: true }).click();
    await expect(modal.getByLabel("Meeting status")).toHaveValue(
      "needs_review",
    );
    await expect(
      modal.getByRole("button", { name: "Save meeting outcome", exact: true }),
    ).toBeDisabled();
    await modal.getByLabel("Meeting status").selectOption("cancelled");
    await expect(
      modal.getByRole("button", { name: "Save meeting outcome", exact: true }),
    ).toBeDisabled();
    await modal.getByLabel("Next outreach action").selectOption("resume");
    let meetingAttempts = 0;
    await page.route("**/v1/leads/*/meeting", async (route) => {
      meetingAttempts++;
      if (meetingAttempts <= 2)
        await route.fulfill({
          status: 409,
          json: {
            detail:
              "Prospect context changed; refresh before saving your review",
          },
        });
      else await route.continue();
    });
    const refreshed = page.waitForResponse((r) =>
      r.url().endsWith(`/v1/leads/${packet.lead.id}/packet`),
    );
    await modal
      .getByRole("button", { name: "Save meeting outcome", exact: true })
      .click();
    await expect(
      page.getByText(
        "Prospect context changed; refresh before saving your review",
        { exact: true },
      ),
    ).toBeVisible();
    await refreshed;
    expect(meetingAttempts).toBe(1);
    await expect(modal.getByLabel("Meeting status")).toHaveValue("cancelled");
    await expect(modal.getByLabel("Next outreach action")).toHaveValue(
      "resume",
    );
    // A post-call continuity refresh can cause another legitimate conflict after
    // the first review. Re-read the preserved draft before explicitly retrying.
    let meetingSaved = false;
    for (let attempt = 0; attempt < 4; attempt++) {
      const receipt = page.waitForResponse(
        (r) =>
          r.url().endsWith(`/v1/leads/${packet.lead.id}/meeting`) &&
          r.request().method() === "POST",
      );
      const packetReloaded = page.waitForResponse((r) =>
        r.url().endsWith(`/v1/leads/${packet.lead.id}/packet`),
      );
      await modal
        .getByRole("button", { name: "Save meeting outcome", exact: true })
        .click();
      const response = await receipt;
      await (await packetReloaded).finished();
      if (response.status() === 200) {
        meetingSaved = true;
        break;
      }
      expect(response.status()).toBe(409);
      expect((await response.json()).detail).toBe(
        "Prospect context changed; refresh before saving your review",
      );
      await expect(modal.getByLabel("Meeting status")).toHaveValue("cancelled");
      await expect(modal.getByLabel("Next outreach action")).toHaveValue(
        "resume",
      );
      await expect(
        modal.getByText(/Keep my original human note through the correction/),
      ).toBeVisible();
    }
    expect(meetingAttempts).toBeGreaterThanOrEqual(3);
    expect(meetingSaved).toBe(true);
    await expect(modal.getByText(/^Outreach paused:/)).toHaveCount(0);
    await expect(
      modal.getByText(/Keep my original human note through the correction/),
    ).toBeVisible();
  });
}

test("a suggested outcome requires a rep choice and keeps the rep's note and contact role", async ({
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
  await prepareSimulationCall(
    page,
    "Suggested outcome fixture",
    "+14155550178",
  );
  await page.route("**/v1/calls/*", async (route) => {
    if (
      route.request().method() !== "GET" ||
      !/\/v1\/calls\/[^/]+$/.test(route.request().url())
    )
      return route.continue();
    const response = await route.fetch();
    const call = await response.json();
    await route.fulfill({
      response,
      json: {
        ...call,
        rundown_status: "partial",
        suggested_outcome: {
          code: "not_interested",
          contact_role: "unknown",
          reason: "The contact declined further discussion.",
          source_observation_id: "synthetic-signal",
          review_receipt_id: "synthetic-review",
        },
      },
    });
  });
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  // No outcome is presumed: the rep chooses one before Save is enabled.
  await expect(page.getByLabel("Disposition")).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Save outcome", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Use suggested outcome", exact: true })
    .click();
  await expect(page.getByLabel("Disposition")).toHaveValue("not_interested");
  await page.getByLabel("Who did you speak with?").selectOption("gatekeeper");
  await page
    .getByLabel("Wrap-up note")
    .fill("The rep confirmed this was the office assistant.");
  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/disposition") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  const result = await saved;
  expect(result.status()).toBe(200);
  expect(result.request().postDataJSON()).toMatchObject({
    outcome: "not_interested",
    contact_role: "gatekeeper",
    callback_note: "The rep confirmed this was the office assistant.",
  });
});

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
    await chooseOutcome(page, "callback_requested");
    const saved = page.waitForResponse(
      (r) =>
        r.url().endsWith("/disposition") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Save outcome" }).click();
    const call = await (await saved).json();
    await page.getByRole("button", { name: "Review", exact: true }).click();
    const task = page.locator(`[data-call-id="${call.id}"]`);
    await task.getByLabel(/^Callback time/).fill("2026-10-22T13:30");
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
    await task.getByLabel(/^Reminder until/).fill("2026-10-21T11:00");
    const snoozed = page.waitForResponse(
      (r) => r.url().includes("/v1/tasks/") && r.request().method() === "PATCH",
    );
    await task
      .getByRole("button", { name: "Snooze reminder", exact: true })
      .click();
    const result = await (await snoozed).json();
    expect(Date.parse(result.due_at)).toBe(
      Date.parse(String(requests[0].due_at)),
    );
    expect(result.snoozed_until).not.toBe(result.due_at);
    await page.reload();
    await page.getByRole("button", { name: "Review", exact: true }).click();
    await expect(task.getByLabel(/^Callback time/)).toHaveValue(
      "2026-10-22T13:30",
    );
    await expect(task.getByLabel(/^Reminder until/)).toHaveValue(
      "2026-10-21T11:00",
    );
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
  await page.getByRole("button", { name: "Review", exact: true }).click();
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

test("an AI-suggested callback shows its confirmation deadline apart from confirmed callbacks", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const workspace = await response.json();
    const lead = workspace.leads[0];
    lead.timezone = "America/New_York";
    const base = {
      lead_id: lead.id,
      owner_id: workspace.user.id,
      kind: "callback",
      timezone: "America/New_York",
    };
    workspace.tasks = [
      {
        ...base,
        id: "suggested-task",
        version: 1,
        call_id: "suggested-source-call",
        action: "Confirm callback time",
        due_at: null,
        status: "needs_confirmation",
        confirmation_state: "suggested",
        source: "inferred",
        callback_suggestion_until: "2026-10-21T18:00:00+00:00",
      },
      {
        ...base,
        id: "confirmed-task",
        version: 1,
        call_id: "confirmed-source-call",
        action: "Call back",
        due_at: "2026-10-22T18:00:00+00:00",
        status: "open",
        confirmation_state: "confirmed",
        source: "rep_reported",
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
  await page.getByRole("button", { name: "Review", exact: true }).click();
  const suggested = page.locator('[data-call-id="suggested-source-call"]');
  // Shown in the lead's zone: 18:00 UTC is 2:00 PM in New York.
  await expect(
    suggested.getByText("Suggested callback, confirm by Oct 21, 2:00 PM EDT", {
      exact: false,
    }),
  ).toBeVisible();
  const confirmed = page.locator('[data-call-id="confirmed-source-call"]');
  await expect(confirmed.getByText("Open", { exact: true })).toBeVisible();
  await expect(confirmed.getByText(/Suggested callback/)).toHaveCount(0);
});

test("a callback time that a person already handled names the follow-up to review", async ({
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
  await prepareSimulationCall(page, "Callback review fixture", "+14155550886");
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "callback_requested");
  await page.getByLabel(/^Confirmed callback time/).fill("2026-10-23T10:30");
  let taskId = "";
  await page.route("**/v1/calls/*/disposition", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    taskId = body.canonical_task_id;
    // The authority kept a person's edit and reports it instead of overwriting.
    body.callback_review = {
      status: "needs_review",
      reason:
        "A person already edited or closed this callback; review the new time.",
      task_ids: [taskId],
    };
    await route.fulfill({ response, json: body });
  });
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  const review = page.getByRole("region", { name: "Callback needs review" });
  await expect(
    review.getByText(
      "Outcome saved. The callback time was not applied: A person already edited or closed this callback; review the new time.",
    ),
  ).toBeVisible();
  await review.getByRole("button", { name: "Review follow-up" }).click();
  await expect(page.locator(`#followup-task-${taskId}`)).toBeFocused();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await page.getByRole("button", { name: "Pause dialing" }).click();
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
  await importLead(page, "Local callback fixture", "+14155550703");
  if (await page.getByRole("button", { name: "Pause dialing" }).isVisible())
    await page.getByRole("button", { name: "Pause dialing" }).click();
  await page.getByRole("button", { name: "Go ready" }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "callback_requested");
  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/disposition") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Save outcome" }).click();
  const call = await (await saved).json();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  const task = page.locator(`[data-call-id="${call.id}"]`);
  await expect(
    task.getByText("Needs confirmation", { exact: true }),
  ).toBeVisible();
  await task.getByLabel(/^Callback time/).fill("2026-10-21T11:00");
  await task.getByRole("button", { name: "Save follow-up" }).click();
  await expect(task.getByText("Open", { exact: true })).toBeVisible();
  await task.getByLabel(/^Callback time/).fill("2026-10-22T13:30");
  await task.getByRole("button", { name: "Save follow-up" }).click();
  await page.reload();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await expect(task.getByLabel(/^Callback time/)).toHaveValue(
    "2026-10-22T13:30",
  );
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
      "name,phone,company,timezone,region\nRestart Fixture,+14155550119,Restart Fixture Co,America/Los_Angeles,415",
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/1 imported/)).toBeVisible();
  await waitForPreparation(page);
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  if (await page.getByRole("button", { name: "Go ready" }).isVisible())
    await page.getByRole("button", { name: "Go ready" }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save outcome", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save outcome", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Save outcome", exact: true }),
  ).toBeVisible();
  await chooseOutcome(page, "no_answer");
  const sent = page.waitForRequest(
    (r) => r.url().endsWith("/disposition") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "Save outcome" }).click();
  expect((await sent).postDataJSON().expected_version).toBeGreaterThan(0);
  await expect(
    page.getByText("Outcome saved. Analysis continues in the background."),
  ).toBeVisible();
});

test("outcome conflict reload and late save preserve the selected review", async ({
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
  await prepareSimulationCall(page, "Review response fixture", "+14155550882");
  const calls: { id: string; name: string }[] = [];
  for (let index = 0; index < 2; index++) {
    // A saved wrap-up refreshes its own packet in the background; read the
    // next lead from its reservation instead.
    const reserved = page.waitForResponse((r) =>
      r.url().endsWith("/v1/queue/next"),
    );
    await page
      .getByRole("button", { name: "Get next lead", exact: true })
      .click();
    const name = (await (await reserved).json()).lead.name;
    const started = page.waitForResponse((r) =>
      r.url().endsWith("/v1/calls/start"),
    );
    await page.getByRole("button", { name: "Start call", exact: true }).click();
    const call = await (await started).json();
    calls.push({ id: call.id, name });
    await page.getByRole("button", { name: "Hang up", exact: true }).click();
    await chooseOutcome(page, "no_answer");
    await page.getByLabel("Wrap-up note").fill(`Human note ${index}`);
    await page
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Save outcome", exact: true }),
    ).toBeHidden();
  }
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  // The queue may offer the same lead twice, so select each call by identity.
  await page
    .locator(`tr[data-call-id="${calls[0].id}"]`)
    .getByRole("button", { name: `Review call ${calls[0].name}`, exact: true })
    .click();
  await page.getByText("Correct confirmed outcome", { exact: true }).click();
  await chooseOutcome(page, "right_party");
  let attempts = 0;
  let release!: () => void;
  let received!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const arrived = new Promise<void>((resolve) => {
    received = resolve;
  });
  await page.route(`**/v1/calls/${calls[0].id}/disposition`, async (route) => {
    attempts++;
    if (attempts === 1) {
      await route.fulfill({
        status: 409,
        json: { detail: "Call changed; reload before saving" },
      });
      return;
    }
    const response = await route.fetch();
    expect(response.ok()).toBeTruthy();
    received();
    await held;
    await route.fulfill({ response });
  });
  try {
    await page
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    await expect(
      page.getByText("Call changed; reload before saving", { exact: true }),
    ).toBeVisible();
    // A persistent error toast would cover the second call's review action.
    await page.getByRole("button", { name: "Dismiss notification" }).click();
    await expect(page.getByLabel("Wrap-up note")).toHaveValue("Human note 0");
    const refreshed = page.waitForResponse((r) =>
      r.url().endsWith(`/v1/calls/${calls[0].id}/analysis`),
    );
    await page
      .getByRole("button", { name: "Reload confirmed outcome", exact: true })
      .click();
    await refreshed;
    await chooseOutcome(page, "right_party");
    await page
      .getByRole("button", { name: "Save outcome", exact: true })
      .click();
    await arrived;
    await page
      .getByRole("button", { name: "All conversations", exact: true })
      .click();
    await page
      .locator(`tr[data-call-id="${calls[1].id}"]`)
      .getByRole("button", {
        name: `Review call ${calls[1].name}`,
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("heading", { name: calls[1].name, exact: true }),
    ).toBeVisible();
    release();
    await page.unrouteAll({ behavior: "wait" });
    await expect(
      page.getByText("Outcome saved. Analysis continues in the background.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: calls[1].name, exact: true }),
    ).toBeVisible();
    await page.getByText("Correct confirmed outcome", { exact: true }).click();
    await expect(page.getByLabel("Wrap-up note")).toHaveValue("Human note 1");
    await expect(page.getByLabel("Disposition")).toHaveValue("no_answer");
    expect(attempts).toBe(2);
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});
