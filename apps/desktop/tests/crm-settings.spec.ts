import { expect, test } from "./fixtures";
import { isolatedSimulationWorkspace } from "./simulation";
import { noSidewaysScroll, uncovered } from "./layout";

test("the CRM keys save and check in Settings, and the HubSpot panel names a missing key", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await isolatedSimulationWorkspace(page, "crm-settings");
  await page.setViewportSize({ width: 1024, height: 720 });
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  // R9B.12: the HubSpot panel and the new Settings rows pass the layout audit at the small size.
  const choose = page.getByRole("button", {
    name: "Choose a HubSpot list",
    exact: true,
  });
  await choose.scrollIntoViewIfNeeded();
  await uncovered(choose);
  await noSidewaysScroll(page);
  await page
    .getByRole("button", { name: "Choose a HubSpot list", exact: true })
    .click();
  await expect(
    page.getByText("Add the HubSpot Service Key in Settings first").first(),
  ).toBeVisible();

  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const dnc = page.getByTestId("setup-realvalidito");
  await expect(dnc).toContainText("Do-not-call check on CRM leads");
  await dnc.getByLabel("New RealValidito key").fill("rv-key-test");
  const save = dnc.getByRole("button", { name: "Save and check" });
  await expect(save).toBeDisabled();
  await dnc.getByLabel("New RealValidito secret").fill("rv-secret-test");
  await save.click();
  await expect(
    dnc.getByRole("status", { name: "RealValidito key and secret status" }),
  ).toContainText("Checked");
  for (const [id, label] of [
    ["hubspot", "HubSpot Service Key"],
    ["treg", "Treg token"],
  ]) {
    const row = page.getByTestId(`setup-${id}`);
    await row.getByLabel(`New ${label}`).fill(`${id}-test-value`);
    await row.getByRole("button", { name: "Save and check" }).click();
    await expect(
      row.getByRole("status", { name: `${label} status` }),
    ).toContainText("Checked");
  }
  await save.scrollIntoViewIfNeeded();
  await uncovered(save);
  await noSidewaysScroll(page);
  await expect(page.getByTestId("setup-calls_for")).toContainText(
    "Which HubSpot contacts this computer syncs and calls",
  );
});
