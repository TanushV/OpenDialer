import { expect, test, type Page } from "./fixtures";

async function activeHelper(page: Page, coach?: Record<string, unknown>) {
  let call: Record<string, unknown> = {};
  await page.route("**/v1/workspace", async (route) => {
    // The app can abort a workspace read it has superseded while this awaits the real one.
    const response = await route.fetch().catch(() => null);
    if (!response) return;
    const data = await response.json();
    call = {
      id: "helper-cancel-call",
      version: 1,
      // A real call always carries its transcript version; updates compare it.
      transcript_version: 0,
      lead_id: data.leads[0].id,
      rep_id: data.user.id,
      state: "HUMAN",
      transcript: [],
      created_at: new Date().toISOString(),
      coach,
    };
    data.calls = [call];
    // A refresh the app supersedes is aborted while this handler awaits the real response;
    // fulfilling that request then fails with "Route is already handled" (CI run 39682453717).
    await route.fulfill({ response, json: data }).catch((error: Error) => {
      if (!error.message.includes("Route is already handled")) throw error;
    });
  });
  await page.route("**/v1/calls/helper-cancel-call", (route) =>
    route.fulfill({ json: call }),
  );
  const help = page.getByRole("button", { name: "Private help", exact: true });
  for (let attempt = 0; ; attempt++) {
    await page.goto("/");
    await expect(page.getByText("Opening your workspace…")).toBeHidden();
    const enter = page.getByRole("button", {
      name: "Try a simulated workspace",
    });
    if (await enter.isVisible()) await enter.click();
    // The injected call rides on a workspace read the app may abort and replace while this
    // fixture awaits the real response (CI runs 39682453717, 39689277229): reopen until it shows.
    const shown = await help.waitFor({ timeout: 8000 }).then(
      () => true,
      () => false,
    );
    if (shown) break;
    if (attempt === 2)
      throw new Error("The injected helper call never appeared");
  }
  await help.click();
  return () => {
    coach = undefined;
    delete call.coach;
  };
}

test("a complete private suggestion stays readable while its explanation is finishing", async ({
  page,
}) => {
  const coach = {
    id: "streamed-private-answer",
    status: "streaming",
    private: true,
    say_or_ask: "Which part of the approval process should we clarify?",
    reason: "",
    remaining_ms: 300000,
    raw_expires_at: new Date(Date.now() + 300000).toISOString(),
    expires_at: new Date(Date.now() + 300000).toISOString(),
  };
  await activeHelper(page, coach);
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/calls/*/coach", async (route) => {
    await delayed;
    coach.status = "ready";
    coach.reason = "Clarifying the approval process avoids making assumptions.";
    await route.fulfill({ json: { status: "ready" } });
  });
  try {
    await page
      .getByRole("button", { name: "Ask privately", exact: true })
      .click();
    await expect(
      page.getByText(coach.say_or_ask, { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Answer ready · finishing explanation…", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Cancel help", exact: true }),
    ).toBeEnabled();
    release();
    await expect(
      page.getByText(
        "Clarifying the approval process avoids making assumptions.",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(
      page.getByText("Answer ready · finishing explanation…", { exact: true }),
    ).toBeHidden();
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("cancelling refreshes an answer delivered before its HTTP response finishes", async ({
  page,
}) => {
  const remove = await activeHelper(page, {
    id: "published-private",
    say_or_ask: "This private answer is now cancelled.",
    private: true,
    remaining_ms: 300000,
    raw_expires_at: new Date(Date.now() + 300000).toISOString(),
    expires_at: new Date(Date.now() + 300000).toISOString(),
  });
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/calls/*/coach", async (route) => {
    await delayed;
    await route.fulfill({ json: { action: "NO_ACTION" } });
  });
  await page.route("**/v1/calls/*/coach/cancel", (route) => {
    remove();
    return route.fulfill({ json: { status: "cancelled" } });
  });
  try {
    await expect(
      page.getByText("This private answer is now cancelled.", { exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Ask privately", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Cancel help", exact: true })
      .click();
    await expect(
      page.getByText("This private answer is now cancelled.", { exact: true }),
    ).toBeHidden();
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("private help can be cancelled without a late response clearing a newer request", async ({
  page,
}) => {
  await activeHelper(page);
  const pending: { body: { request_id: string }; release: () => void }[] = [];
  const cancelled: string[] = [];
  await page.route("**/v1/calls/*/coach", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await new Promise<void>((resolve) =>
      pending.push({ body: route.request().postDataJSON(), release: resolve }),
    );
    await route.fulfill({ json: { action: "NO_ACTION", status: "no_action" } });
  });
  await page.route("**/v1/calls/*/coach/cancel", (route) => {
    cancelled.push(route.request().postDataJSON().request_id);
    return route.fulfill({ json: { status: "cancelled" } });
  });
  try {
    await page
      .getByLabel("Private helper question")
      .fill("First private question?");
    await page
      .getByRole("button", { name: "Ask privately", exact: true })
      .click();
    await expect.poll(() => pending.length).toBe(1);
    await page
      .getByRole("button", { name: "Cancel help", exact: true })
      .click();
    expect(cancelled).toEqual([pending[0].body.request_id]);
    expect(cancelled[0]).toMatch(/^[\da-f-]{36}$/);
    await page
      .getByLabel("Private helper question")
      .fill("Second private question?");
    await page
      .getByRole("button", { name: "Ask privately", exact: true })
      .click();
    await expect.poll(() => pending.length).toBe(2);
    const response = page.waitForResponse(
      (r) => r.url().endsWith("/coach") && r.request().method() === "POST",
    );
    pending[0].release();
    await response;
    await expect(
      page.getByRole("button", { name: "Cancel help", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Working…", exact: true }),
    ).toBeDisabled();
    await page
      .getByRole("button", { name: "Cancel help", exact: true })
      .click();
    expect(cancelled[1]).toBe(pending[1].body.request_id);
  } finally {
    pending.forEach((p) => p.release());
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("the help hotkey has one cancellable request and respects key repeats", async ({
  page,
}) => {
  await activeHelper(page);
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  let requests = 0;
  await page.route("**/v1/calls/*/coach", async (route) => {
    requests++;
    await delayed;
    await route.fulfill({ json: { action: "NO_ACTION" } });
  });
  await page.route("**/v1/calls/*/coach/cancel", (route) =>
    route.fulfill({ json: { status: "cancelled" } }),
  );
  try {
    await page.keyboard.press("F1");
    await expect.poll(() => requests).toBe(1);
    await page.keyboard.press("F1");
    await expect(
      page.getByRole("button", { name: "Cancel help", exact: true }),
    ).toBeVisible();
    expect(requests).toBe(1);
    await page
      .getByRole("button", { name: "Cancel help", exact: true })
      .click();
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});
