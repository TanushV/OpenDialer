import { expect, test } from "./fixtures";
import { openMoreSettings, openSettingRow } from "./navigation";
import { isolatedSimulationWorkspace } from "./simulation";

test("a late duplicate callback routing read cannot erase a typed greeting", async ({
  page,
}) => {
  let reads = 0;
  let release!: () => void;
  let delivered!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const late = new Promise<void>((resolve) => {
    delivered = resolve;
  });
  await page.route("**/v1/admin/callback-routing", async (route) => {
    if (route.request().method() !== "GET" || ++reads !== 1)
      return route.continue();
    const response = await route.fetch();
    await held;
    await route.fulfill({ response });
    delivered();
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await openMoreSettings(page, "Callbacks");
  const field = page
    .getByRole("region", { name: "Inbound callbacks", exact: true })
    .getByRole("textbox", { name: /Missed-call greeting/ });
  await field.fill("Typed before the late read.");
  release();
  await late;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await expect(field).toHaveValue("Typed before the late read.");
});

test("the callback greeting survives reload and the card says where callbacks ring", async ({
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await isolatedSimulationWorkspace(page, "callback-routing-fixture");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await openMoreSettings(page, "Callbacks");
  const panel = page.getByRole("region", {
    name: "Inbound callbacks",
    exact: true,
  });
  // D29 and D30: callbacks ring the open app; forwarding to a phone is not offered yet.
  await expect(panel).toContainText(
    "While this app is open, a callback rings here on any page",
  );
  await expect(panel.getByLabel("Business fallback destination")).toHaveCount(
    0,
  );
  const greeting = panel.getByRole("textbox", { name: /Missed-call greeting/ });
  await expect(greeting).toHaveValue("");
  await greeting.fill("You've reached our reception. We'll return your call.");
  await panel.getByRole("button", { name: "Save callbacks" }).click();
  await expect(panel).toContainText("Callbacks saved");
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await openMoreSettings(page, "Callbacks");
  await expect(greeting).toHaveValue(
    "You've reached our reception. We'll return your call.",
  );
  await greeting.clear();
  await panel.getByRole("button", { name: "Save callbacks" }).click();
  await expect(panel).toContainText("Callbacks saved");
});

test("a routed inbound call has visible controls and blocks the outbound next button", async ({
  page,
}) => {
  let ended = false;
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.tasks.push({
      id: "inbound-visible",
      version: 1,
      inbound: true,
      inbound_state: ended ? "ended" : "connected",
      routed_rep_id: data.user.id,
      callback_phone: "+14155550111",
      lead_id: null,
      status: "completed",
      due_at: null,
      action: "Review return call",
      route: "responsible_rep",
    });
    await route.fulfill({ response, json: data });
  });
  await page.route("**/v1/callbacks/inbound-visible/hangup", async (route) => {
    ended = true;
    await route.fulfill({ json: { inbound_state: "ending" } });
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  const inbound = page.getByRole("region", {
    name: "Incoming call",
    exact: true,
  });
  await expect(inbound).toContainText("+14155550111");
  await expect(
    inbound.getByRole("button", { name: "Mute", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: /^Start calling/ }),
  ).toBeDisabled();
  await inbound.getByRole("button", { name: "Hang up", exact: true }).click();
  await expect(inbound).toBeHidden();
  expect(ended).toBe(true);
});

test("ringing return call exposes explicit acceptance and waits for the matching audio leg", async ({
  page,
}) => {
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.tasks.push({
      id: "ringing-return",
      version: 1,
      inbound: true,
      inbound_state: "ringing",
      routed_rep_id: data.user.id,
      callback_phone: "+14155550111",
      lead_id: null,
      status: "open",
      due_at: null,
      action: "Return call",
      route: "responsible_rep",
      routing_attempts: [
        {
          id: "ring-one",
          rep_id: data.user.id,
          state: "ringing",
          provider_id: "verified-return-leg",
        },
      ],
    });
    await route.fulfill({ response, json: data });
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await isolatedSimulationWorkspace(page, "ringing-return-fixture");
  const inbound = page.getByRole("region", {
    name: "Incoming call",
    exact: true,
  });
  // D29: it rings at once; Answer waits for this app's own leg, and Decline is always there.
  await expect(inbound).toHaveAttribute("data-ringing", "true");
  await expect(
    inbound.getByRole("button", { name: "Answer", exact: true }),
  ).toBeDisabled();
  await expect(inbound).toContainText("Connecting the call to this app");
  await expect(
    inbound.getByRole("button", { name: "Decline", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: /^Start calling/ }),
  ).toBeDisabled();
});

test("a callback rings from any page and the open app says it can take calls", async ({
  page,
}) => {
  // D29, owner on November 10: every callback heard the greeting because the app was never rung.
  const reachable: unknown[] = [];
  await page.route("**/v1/rep/reachable", async (route) => {
    reachable.push(route.request().postDataJSON());
    await route.continue();
  });
  let arrived = false;
  await page.route("**/v1/workspace", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    if (arrived)
      data.tasks.push({
        id: "ringing-anywhere",
        version: 1,
        inbound: true,
        inbound_state: "ringing",
        routed_rep_id: data.user.id,
        callback_phone: "+14155550111",
        lead_name: "Jordan Ellis",
        lead_company: "Northstar Example",
        lead_id: null,
        status: "open",
        due_at: null,
        action: "Return call",
        route: "rep",
        routing_attempts: [
          {
            id: "ring-any",
            rep_id: data.user.id,
            state: "ringing",
            provider_id: "leg-any",
          },
        ],
      });
    await route.fulfill({ response, json: data });
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await expect.poll(() => reachable.length).toBeGreaterThan(0);
  expect(reachable[0]).toEqual({ registered: true });
  await openSettingRow(page, "hours");
  arrived = true;
  const inbound = page.getByRole("region", {
    name: "Incoming call",
    exact: true,
  });
  // Saving re-reads the workspace; a background re-read may bring the call first and replace the page.
  await page
    .getByRole("button", { name: "Save calling hours", exact: true })
    .click({ timeout: 5_000 })
    .catch(async (error) => {
      if (!(await inbound.isVisible())) throw error;
    });
  await expect(inbound).toBeVisible();
  await expect(inbound).toHaveAttribute("data-ringing", "true");
  await expect(inbound).toContainText("Jordan Ellis · Northstar Example");
  await expect(inbound).toContainText("+14155550111");
});
