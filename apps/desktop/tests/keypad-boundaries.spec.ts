import { expect, test, type Page } from "./fixtures";

async function activeKeypad(page: Page) {
  let storageKey = "";
  let call: Record<string, unknown> = {};
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    storageKey = `opendialer.keypad-${data.user.id}`;
    call = {
      id: "keypad-boundary-call",
      version: 1,
      lead_id: data.leads[0].id,
      rep_id: data.user.id,
      state: "IVR",
      created_at: new Date().toISOString(),
    };
    data.calls = [call];
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/calls/keypad-boundary-call", (route) =>
    route.fulfill({ json: call }),
  );
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const enter = page.getByRole("button", { name: "Try a simulated workspace" });
  if (await enter.isVisible()) await enter.click();
  await expect(page.getByRole("button", { name: /Keypad$/ })).toBeVisible();
  return storageKey;
}

test("unreadable keypad cache blocks new tones until successfully reopened", async ({
  page,
}) => {
  const key = await activeKeypad(page);
  await page.evaluate((key) => sessionStorage.setItem(key, "{broken"), key);
  await page.getByRole("button", { name: /Keypad$/ }).click();
  await expect(
    page.getByText(/Saved keypad state could not be read/),
  ).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Call keypad" })
      .getByRole("button", { name: "1", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Close keypad" }).click();
  await page.evaluate((key) => sessionStorage.removeItem(key), key);
  await page.getByRole("button", { name: /Keypad$/ }).click();
  await expect(
    page
      .getByRole("region", { name: "Call keypad" })
      .getByRole("button", { name: "1", exact: true }),
  ).toBeEnabled();
});

test("old unreceived keypad presses require a new menu choice instead of replay", async ({
  page,
}) => {
  const key = await activeKeypad(page);
  await page.route("**/v1/calls/*/dtmf/*", (route) =>
    route.fulfill({ status: 404, json: { detail: "Not received" } }),
  );
  let submissions = 0;
  await page.route("**/v1/calls/*/dtmf", (route) => {
    submissions++;
    return route.fulfill({ json: { status: "completed", sent: true } });
  });
  await page.evaluate(
    (key) =>
      sessionStorage.setItem(
        key,
        JSON.stringify({
          call_id: "keypad-boundary-call",
          idempotency_key: "old-press",
          digits: "9",
          created_at: Date.now() - 60_000,
        }),
      ),
    key,
  );
  await page.getByRole("button", { name: /Keypad$/ }).click();
  await expect(
    page.getByRole("button", { name: "Retry same keypad request" }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "I checked the menu; allow a new key" })
    .click();
  expect(submissions).toBe(0);
  await expect(
    page
      .getByRole("region", { name: "Call keypad" })
      .getByRole("button", { name: "1", exact: true }),
  ).toBeEnabled();
});

test("a late response from a closed keypad cannot erase its newer pending press", async ({
  page,
}) => {
  const key = await activeKeypad(page);
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  let submissions = 0;
  await page.route("**/v1/calls/*/dtmf", async (route) => {
    submissions++;
    if (submissions === 1) {
      await delayed;
      await route.fulfill({ json: { status: "completed", sent: true } });
    } else await route.abort("failed");
  });
  await page.route("**/v1/calls/*/dtmf/*", (route) =>
    route.fulfill({ json: { status: "completed", sent: true } }),
  );
  await page.getByRole("button", { name: /Keypad$/ }).click();
  const digit = (d: string) =>
    page
      .getByRole("region", { name: "Call keypad" })
      .getByRole("button", { name: d, exact: true });
  await digit("1").click();
  await expect.poll(() => submissions).toBe(1);
  await page.getByRole("button", { name: "Close keypad" }).click();
  await page.getByRole("button", { name: /Keypad$/ }).click();
  await expect(digit("2")).toBeEnabled();
  await digit("2").click();
  await expect(
    page.getByRole("button", { name: "Retry same keypad request" }),
  ).toBeVisible();
  const pending = await page.evaluate(
    (key) => sessionStorage.getItem(key),
    key,
  );
  const response = page.waitForResponse(
    (r) => r.url().endsWith("/dtmf") && r.ok(),
  );
  release();
  await response;
  // The original request has completed, but the newly saved request still needs reconciliation.
  await expect
    .poll(() => page.evaluate((key) => sessionStorage.getItem(key), key))
    .toBe(pending);
  await page.getByRole("button", { name: "Close keypad" }).click();
  await expect
    .poll(() => page.evaluate((key) => sessionStorage.getItem(key), key))
    .toBe(pending);
});
