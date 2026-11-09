import { expect, test } from "./fixtures";
import { openLeads } from "./simulation";

const CSV = [
  "name,phone,company,timezone,title,finder,network status,signal summaries,signal dates,signal urls,email",
  'Liam Intake,+16175550123,Brighterway,America/New_York,VP Sales,blitzapi,LIVE,"Hiring SDRs | New office","2026-10-20; 2026-09-02","https://b.example/jobs; https://b.example/news",liam@b.example',
  "Dana Dead,+16175550124,Brighterway,America/New_York,Director,wiza,DEAD,,,,dana@b.example",
].join("\n");

test("import shows the column map, keeps per-column separators and marks a dead number", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await openLeads(page);
  await page.getByLabel("List name", { exact: true }).fill("Intake check");
  await page.getByLabel("CSV lead data").fill(CSV);
  const map = page.getByLabel("Column map");
  await expect(
    map.getByRole("combobox", { name: "Signal summaries", exact: true }),
  ).toHaveValue("signal summaries");
  await expect(map.getByLabel("Signal summaries separator")).toHaveValue("|");
  await expect(map.getByLabel("Signal links separator")).toHaveValue(";");
  await expect(
    map.getByRole("combobox", {
      name: "Live status (DEAD is never dialed)",
    }),
  ).toHaveValue("network status");
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/2 imported/)).toBeVisible();
  await expect(
    page.getByText("Not dialed · The list marks this number dead", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(page.getByText("Not checked (CSV)").first()).toBeVisible();
});

test("the guessed column map arrives below Import leads and never moves the button", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await openLeads(page);
  // Hold the guess so the button can be measured before and after the map appears.
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/v1/leads/column-map", async (route) => {
    await held;
    await route.continue();
  });
  const button = page.getByRole("button", {
    name: "Import leads",
    exact: true,
  });
  try {
    await page.getByLabel("CSV lead data").fill(CSV);
    await expect(button).toBeEnabled();
    // Measured in the page, not the window, so a scroll cannot pass for a shift.
    const top = () =>
      button.evaluate((b) => b.getBoundingClientRect().top + window.scrollY);
    const before = await top();
    release();
    await expect(page.getByLabel("Column map")).toBeVisible();
    expect(await top()).toBe(before);
  } finally {
    release();
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
});
