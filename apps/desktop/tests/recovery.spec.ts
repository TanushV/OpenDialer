import { expect, test } from "./fixtures";
import { waitForPreparation } from "./simulation";
import { openMoreSettings } from "./navigation";

test("number route recovery preserves its original target and does not offer call controls", async ({
  page,
}) => {
  let complete = false;
  let checked = 0;
  await page.route("**/v1/admin/recovery", async (route) =>
    route.fulfill({
      json: {
        commands: [],
        agents: [],
        jobs: [],
        limited: false,
        operations: [
          ...(!complete
            ? [
                {
                  id: "route-intent",
                  version: 4,
                  kind: "number_configure",
                  status: "review_required",
                  provider_id: "owned-number",
                  connection_id: "original-route",
                  number_id: "local-number",
                  reason:
                    "Verify the original number route; callback testing is separate.",
                  can_reconcile: true,
                },
              ]
            : []),
          {
            id: "old-batch",
            version: 2,
            kind: "script_batch",
            status: "uncertain",
            provider_id: null,
            reason:
              "Review the original deferred preparation request; do not submit another batch.",
            can_reconcile: false,
          },
        ],
      },
    }),
  );
  await page.route(
    "**/v1/admin/recovery/commands/route-intent",
    async (route) => {
      checked++;
      expect(route.request().postDataJSON()).toEqual({
        expected_version: 4,
        action: "reconcile",
      });
      if (checked === 1)
        await route.fulfill({
          status: 503,
          json: { detail: "Number status unavailable" },
        });
      else {
        complete = true;
        await route.fulfill({ json: { status: "completed" } });
      }
    },
  );
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await openMoreSettings(page, "Users, backups and recovery");
  const recovery = page.getByRole("region", {
    name: "Work needing recovery",
    exact: true,
  });
  await expect(recovery).toContainText("original-route");
  await expect(recovery).toContainText("deferred preparation request");
  await expect(
    recovery.getByRole("button", { name: "End existing leg" }),
  ).toHaveCount(0);
  await expect(recovery.getByLabel("Original carrier reference")).toHaveCount(
    0,
  );
  const verify = recovery.getByRole("button", {
    name: "Verify original number route",
    exact: true,
  });
  await verify.click();
  await expect(recovery).toContainText("Number status unavailable");
  await expect(verify).toBeEnabled();
  await verify.click();
  await expect(verify).toHaveCount(0);
  await expect(recovery).not.toContainText("No work needs recovery");
  expect(checked).toBe(2);
});

test("recovery offers verified reconciliation and keeps failed actions retryable", async ({
  page,
}) => {
  let reconciled = false;
  let submissions = 0;
  await page.route("**/v1/admin/recovery", async (route) => {
    await route.fulfill({
      json: {
        commands: reconciled
          ? []
          : [
              {
                id: "uncertain-command",
                version: 3,
                kind: "dial",
                status: "review_required",
                call_id: "uncertain-call",
                provider_id: null,
                reason: "No matching carrier status receipt",
                can_end: false,
                can_cancel: false,
              },
            ],
        agents: [],
        jobs: [],
        limited: false,
      },
    });
  });
  await page.route(
    "**/v1/admin/recovery/commands/uncertain-command",
    async (route) => {
      submissions++;
      expect(route.request().postDataJSON()).toEqual({
        expected_version: 3,
        action: "reconcile",
        provider_id: "original-carrier-reference",
      });
      if (submissions === 1) {
        await route.fulfill({
          status: 503,
          json: { detail: "Carrier status unavailable" },
        });
      } else {
        reconciled = true;
        await route.fulfill({ json: { status: "completed" } });
      }
    },
  );
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await openMoreSettings(page, "Users, backups and recovery");
  const recovery = page.getByRole("region", {
    name: "Work needing recovery",
    exact: true,
  });
  await expect(recovery).toContainText("No matching carrier status receipt");
  await recovery
    .getByLabel("Original carrier reference")
    .fill("original-carrier-reference");
  await recovery
    .getByRole("button", { name: "Verify carrier status", exact: true })
    .click();
  await expect(recovery).toContainText("Carrier status unavailable");
  await expect(
    recovery.getByRole("button", {
      name: "Verify carrier status",
      exact: true,
    }),
  ).toBeEnabled();
  await recovery
    .getByRole("button", { name: "Verify carrier status", exact: true })
    .click();
  await expect(recovery).toContainText("No work needs recovery");
  expect(submissions).toBe(2);
});

test("an unconfirmed dial is confirmed not placed only with a recorded reason", async ({
  page,
}) => {
  let resolved = false;
  const bodies: unknown[] = [];
  await page.route("**/v1/admin/recovery", (route) =>
    route.fulfill({
      json: {
        commands: resolved
          ? []
          : [
              {
                id: "unconfirmed-dial",
                version: 5,
                kind: "dial",
                status: "unconfirmed",
                call_id: "held-call",
                provider_id: null,
                reason:
                  "No carrier reference or signed event arrived for this dial. Check the carrier account: attach the call reference if it was placed, or confirm it was not placed.",
                can_end: false,
                can_cancel: false,
                can_confirm_not_placed: true,
              },
            ],
        agents: [],
        jobs: [],
        limited: false,
      },
    }),
  );
  await page.route(
    "**/v1/admin/recovery/commands/unconfirmed-dial",
    async (route) => {
      bodies.push(route.request().postDataJSON());
      resolved = true;
      await route.fulfill({ json: { status: "cancelled" } });
    },
  );
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await openMoreSettings(page, "Users, backups and recovery");
  const recovery = page.getByRole("region", {
    name: "Work needing recovery",
    exact: true,
  });
  await expect(recovery).toContainText(
    "Dial unconfirmed: no carrier reference arrived. The rep is free; this prospect stays held until you resolve it.",
  );
  const confirm = recovery.getByRole("button", {
    name: "Confirm not placed",
    exact: true,
  });
  await expect(confirm).toBeDisabled();
  await recovery
    .getByLabel("Carrier evidence it was not placed")
    .fill("Carrier call log shows no call at 11:02");
  await confirm.click();
  await expect(recovery).toContainText("No work needs recovery");
  expect(bodies).toEqual([
    {
      expected_version: 5,
      action: "confirm_not_placed",
      reason: "Carrier call log shows no call at 11:02",
    },
  ]);
});

test("an administrator releases a temporary safety hold with a recorded reason", async ({
  page,
}) => {
  let authorization = "";
  page.on("request", (request) => {
    const value = request.headers()["authorization"];
    if (value && request.url().startsWith("http://127.0.0.1:8765/"))
      authorization = value;
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
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page
    .getByLabel("CSV lead data")
    .fill(
      "name,phone,company,timezone,region\nHold Fixture,+14155550889,Hold Fixture Co,America/Los_Angeles,415",
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/1 imported/)).toBeVisible();
  await waitForPreparation(page);
  expect(authorization).not.toBe("");
  const held = await page.request.post(
    "http://127.0.0.1:8765/v1/suppressions",
    {
      headers: { authorization },
      data: {
        phone: "+14155550889",
        reason: "temporary_hold",
        event_id: crypto.randomUUID(),
      },
    },
  );
  expect(held.ok()).toBeTruthy();
  await page.reload();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByLabel("Search leads").fill("Hold Fixture");
  await page.getByRole("button", { name: "Search lead history" }).click();
  await page.getByRole("button", { name: "Open lead Hold Fixture" }).click();
  const packet = page.getByRole("dialog", { name: "Lead packet" });
  const release = packet.getByRole("button", {
    name: "Release temporary hold",
    exact: true,
  });
  await expect(release).toBeDisabled();
  await packet
    .getByLabel("Hold review reason")
    .fill("Prospect confirmed they did not ask to stop calls");
  const sent = page.waitForRequest((r) =>
    r.url().endsWith("/v1/admin/suppressions/release-hold"),
  );
  await release.click();
  await packet
    .getByRole("button", { name: "Confirm release", exact: true })
    .click();
  expect((await sent).postDataJSON()).toEqual({
    phone: "+14155550889",
    reason: "Prospect confirmed they did not ask to stop calls",
  });
  await expect(
    page.getByText("Temporary hold released. The reason is recorded."),
  ).toBeVisible();
});
