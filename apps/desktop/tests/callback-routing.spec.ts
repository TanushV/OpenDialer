import { expect, test } from "@playwright/test";

test("callback fallback settings survive reload and explain offline routing", async ({
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const panel = page.getByRole("region", {
    name: "Inbound callbacks",
    exact: true,
  });
  await panel
    .getByLabel("Business fallback destination")
    .fill("sip:reception@example.test");
  await panel.getByRole("button", { name: "Save callback routing" }).click();
  await expect(panel).toContainText("Callback routing saved");
  await expect(panel).toContainText(
    "Carrier-side fallback is required while this computer is offline",
  );
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(panel.getByLabel("Business fallback destination")).toHaveValue(
    "sip:reception@example.test",
  );
  await panel.getByLabel("Business fallback destination").fill("");
  await panel.getByRole("button", { name: "Save callback routing" }).click();
  await expect(panel).toContainText("Callback routing saved");
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
  const inbound = page.getByRole("region", {
    name: "Active return call",
    exact: true,
  });
  await expect(inbound).toContainText("+14155550111");
  await expect(
    inbound.getByRole("button", { name: "Mute return call", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Get next lead", exact: true }),
  ).toBeDisabled();
  await inbound
    .getByRole("button", { name: "End return call", exact: true })
    .click();
  await expect(inbound).toBeHidden();
  expect(ended).toBe(true);
});
