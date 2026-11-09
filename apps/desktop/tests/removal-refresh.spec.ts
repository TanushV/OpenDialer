import { expect, test, type Page } from "./fixtures";
import { isolatedSimulationWorkspace, waitForPreparation } from "./simulation";

async function enter(page: Page, name: string) {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await isolatedSimulationWorkspace(page, name);
}
async function add(page: Page, list: string, csv: string) {
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByLabel("List name", { exact: true }).fill(list);
  await page.getByLabel("CSV lead data").fill(csv);
  await expect(page.getByLabel("List name", { exact: true })).toHaveValue(list);
  const response = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/leads/import") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  const imported = await response;
  expect(imported.ok(), await imported.text()).toBeTruthy();
  await expect(page.getByText(/^\d+ imported/)).toBeVisible();
  await waitForPreparation(page);
  await expect(row(page, csv.split("\n")[1].split(",")[0])).toBeVisible();
}
// Lists and leads share the Leads page (R10.6): a list name is looked up in the lists table, a person in the leads.
const LISTS = new Set(["Remove source 101", "Keep source 101"]);
const row = (page: Page, name: string) =>
  page
    .getByRole("region", { name: LISTS.has(name) ? "Lead lists" : "Leads" })
    .getByRole("row")
    .filter({ has: page.getByText(name, { exact: true }) });

test("remove and restore a whole list while shared contacts stay active", async ({
  page,
}) => {
  await enter(page, "remove-list-101");
  await add(
    page,
    "Remove source 101",
    "name,phone,timezone\nUnique removal,+14155550621,America/Los_Angeles\nShared removal,+14155550622,America/Los_Angeles",
  );
  await add(
    page,
    "Keep source 101",
    "name,phone,timezone\nShared removal,+14155550622,America/Los_Angeles",
  );
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await row(page, "Remove source 101")
    .getByRole("button", { name: "Remove list", exact: true })
    .click();
  await expect(row(page, "Remove source 101")).toBeHidden();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await expect(row(page, "Unique removal")).toBeHidden();
  await expect(row(page, "Shared removal")).toBeVisible();
  await page.getByLabel("Lead view", { exact: true }).selectOption("removed");
  await expect(row(page, "Unique removal")).toBeVisible();
  await expect(row(page, "Shared removal")).toBeHidden();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByLabel("Show removed lists", { exact: true }).check();
  // CI, November 9: a slow restore used to reload the leads with the view shown before the switch below,
  // replacing the Active view with Removed. Delay it so the switch always lands first.
  await page.route("**/v1/lists", async (route) => {
    if (route.request().method() === "PATCH")
      await new Promise((done) => setTimeout(done, 1200));
    await route.continue();
  });
  await row(page, "Remove source 101")
    .getByRole("button", { name: "Restore list", exact: true })
    .click();
  await page.getByRole("button", { name: "Leads", exact: true }).click();
  await page.getByLabel("Lead view", { exact: true }).selectOption("active");
  await expect(row(page, "Unique removal")).toBeVisible();
});

test("remove from a chosen membership and recover an unassigned lead", async ({
  page,
}) => {
  await enter(page, "remove-membership-101");
  await add(
    page,
    "Membership 101",
    "name,phone,timezone\nMembership removal,+14155550623,America/Los_Angeles",
  );
  await page.getByLabel("Filter leads by list").selectOption("Membership 101");
  await page.getByLabel("Select Membership removal", { exact: true }).check();
  await page.getByLabel("Lead action", { exact: true }).selectOption("remove");
  await expect(
    page.getByLabel("List to remove from", { exact: true }),
  ).toHaveValue("Membership 101");
  await page
    .getByRole("button", { name: "Apply to selected leads", exact: true })
    .click();
  await expect(row(page, "Membership removal")).toBeHidden();
  await page.getByLabel("Lead view", { exact: true }).selectOption("removed");
  await expect(row(page, "Membership removal")).toBeVisible();
  await expect(
    row(page, "Membership removal").getByText("No list", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Select Membership removal", { exact: true }).check();
  await page.getByLabel("Lead action", { exact: true }).selectOption("add");
  await page
    .getByLabel("Destination list", { exact: true })
    .fill("Recovered 101");
  await page
    .getByRole("button", { name: "Apply to selected leads", exact: true })
    .click();
  await expect(row(page, "Membership removal")).toBeHidden();
  await page.getByLabel("Lead view", { exact: true }).selectOption("active");
  await expect(row(page, "Membership removal")).toBeVisible();
});

test("script save explains automatic refresh and formatting-only preservation", async ({
  page,
}) => {
  await enter(page, "script-refresh-101");
  await add(
    page,
    "Refresh 101",
    "name,phone,timezone\nRefresh example,+14155550624,America/Los_Angeles",
  );
  await page.getByRole("button", { name: "Script", exact: true }).click();
  const editor = page.getByLabel("Script Markdown");
  await editor.fill(
    "# Opening\nHi {first_name}, {rep} from {seller}.\n# Close\nWould a short conversation help?\n",
  );
  await page.getByRole("button", { name: "Save script", exact: true }).click();
  await expect(
    page.getByText(/Script saved\. Lead scripts refresh automatically/).first(),
  ).toBeVisible();
  const progress = page.getByRole("region", { name: "Script refresh" });
  await expect(progress.getByText(/Master version/)).toBeVisible();
  await expect(progress.getByText(/1 of 1 leads prepared/)).toBeVisible();
  const saved = await editor.inputValue();
  await editor.fill(saved.replace("\n# Close", "\n\n# Close"));
  await page.getByRole("button", { name: "Save script", exact: true }).click();
  await expect(
    page
      .getByText(/Script saved\. Formatting-only edit; prepared scripts kept/)
      .first(),
  ).toBeVisible();
});
