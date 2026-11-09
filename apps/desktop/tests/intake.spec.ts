import { expect, test } from "./fixtures";

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
  await page.getByRole("button", { name: "Leads", exact: true }).click();
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
