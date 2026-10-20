import { expect, test, type Page } from "@playwright/test";

/** An active fixture call whose saved state carries the helper's last receipt. */
async function callWithHelper(page: Page, helper: Record<string, unknown>) {
  let call: Record<string, unknown> = {};
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    call = {
      id: "helper-status-call",
      version: 1,
      transcript_version: 0,
      lead_id: data.leads[0].id,
      rep_id: data.user.id,
      state: "HUMAN",
      transcript: [],
      created_at: new Date().toISOString(),
      conversation_state: {
        stage: "discovery",
        trigger: "prospect",
        version: 3,
        helper_status: { ...helper, at: new Date().toISOString() },
      },
    };
    data.calls = [call];
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/calls/helper-status-call", (route) =>
    route.fulfill({ json: call }),
  );
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const enter = page.getByRole("button", { name: "Try a simulated workspace" });
  if (await enter.isVisible()) await enter.click();
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "wait" });
});

for (const [helper, text] of [
  [
    { status: "unavailable", reason: "transport_fault" },
    "Live help paused · connection interrupted. Keep using your script.",
  ],
  [
    { status: "unavailable", reason: "invalid_response" },
    "Live help paused · last answer could not be used. Keep using your script.",
  ],
  [
    { status: "unavailable", reason: "provider_unverified" },
    "Live help paused · provider not verified. Keep using your script.",
  ],
  [
    { status: "unavailable", reason: "provider_mismatch" },
    "Live help paused · provider route changed. Keep using your script.",
  ],
  [{ status: "cached_knowledge" }, "Using your approved examples"],
  [{ status: "available" }, "Live help available"],
] as const) {
  test(`helper status ${helper.status} ${"reason" in helper ? helper.reason : ""} is a calm status`, async ({
    page,
  }) => {
    await callWithHelper(page, helper);
    const status = page.getByTestId("helper-status");
    await expect(status).toHaveText(text);
    await expect(status).toHaveAttribute("role", "status");
    // A degraded helper is information beside the script, never an alarm.
    await expect(
      page.getByTestId("live-coaching-strip").getByRole("alert"),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Hang up", exact: true }),
    ).toBeVisible();
  });
}
