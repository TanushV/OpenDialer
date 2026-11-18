import { expect, test } from "./fixtures";
import { goTo } from "./navigation";
import { finishCall, openLeads, startCalling } from "./simulation";

const master = `# Paths
- founder: The prospect is a founder.
- sales-leader (default): The prospect runs a sales team.

# Opening
Hello {first_name}, {rep} with {seller}.

# The Question
## Founder [path: founder]
How did you win your first customers?
## Sales leader [path: sales-leader]
How do your reps qualify accounts?
## Shared
What would you change?

# Objections
## Busy
When is better?

# Close
Would Tuesday work?
`;

test("a CSV opens a finished multiline script in Leads and on the call", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await openLeads(page);
  const opening = 'Hi Casey, are you still working on "rush orders"?';
  const script = `# Opening\n${opening}\n\n# Discovery\nWho owns the dispatch schedule?\n\n# Close\nWould Monday work?`;
  await page.getByLabel("Open lead list (.csv)").setInputFiles({
    name: "Finished scripts.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      `name,phone,timezone,Script\nCasey CSV,+14155550127,America/Los_Angeles,"${script.replaceAll('"', '""')}"\n`,
    ),
  });
  await expect(page.getByText(/First row · Casey CSV/)).toBeVisible();
  await page
    .getByRole("button", { name: "Import leads", exact: true })
    .press("Enter");
  await expect(page.getByText("1 script ready", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open lead Casey CSV" }).click();
  const packet = page.getByRole("dialog", { name: "Lead packet" });
  await expect(packet.getByText(opening)).toBeVisible();
  await packet.getByRole("button", { name: "Close", exact: true }).click();

  await goTo(page, "Dialer");
  await page.getByLabel("Dial from all active lists").uncheck();
  await page.getByLabel("Dial list Finished scripts").check();
  try {
    const started = page.waitForResponse((response) =>
      response.url().endsWith("/v1/calls/start"),
    );
    await startCalling(page).click();
    const receipt = await started;
    expect(receipt.ok(), await receipt.text()).toBeTruthy();
    expect((await receipt.json()).script_snapshot.source).toBe("csv");
    const visible = page.getByTestId("prepared-script");
    await expect(visible.getByText(opening)).toBeVisible();
    await expect(visible.getByText("Would Monday work?")).toBeVisible();
  } finally {
    await finishCall(page);
  }
});

test("generated CSV paths match the Dialer and the prospect brief stays on the left", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 720 });
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await goTo(page, "Script");
  await page.getByLabel("Script Markdown").fill(master);
  await page.getByRole("button", { name: "Save script", exact: true }).click();
  await expect(
    page
      .getByRole("region", { name: "Your script", exact: true })
      .getByRole("status")
      .filter({ hasText: /Script saved/ }),
  ).toBeVisible();
  await openLeads(page);
  await page.getByLabel("List name", { exact: true }).fill("Script paths CSV");
  await page
    .getByLabel("CSV lead data")
    .fill(
      [
        "Full Name,Mobile Phone,Company Name,Time Zone,Job Title,Company Description,Account Signals",
        "Avery Path,+14155551840,Brightfield,America/Los_Angeles,Founder,Builds warehouse software,Announced a customer pilot",
        "Jordan Path,+14155551841,Redfield,America/Los_Angeles,Sales Leader,Builds safety software,Opened an Austin office",
      ].join("\n"),
    );
  await page.getByRole("button", { name: "Import leads", exact: true }).click();
  await expect(page.getByText(/^2 imported/)).toBeVisible({ timeout: 30_000 });
  const preparation = page
    .getByRole("region", { name: "Lead preparation" })
    .getByRole("listitem")
    .filter({ hasText: "Script paths CSV" });
  await preparation.getByRole("button", { name: "Pause generation" }).click();
  await expect(preparation.getByText("Generation paused")).toBeVisible();
  await expect(
    preparation.getByRole("button", { name: "Generate scripts" }),
  ).toHaveCount(0);
  await page.reload();
  await openLeads(page);
  await preparation.getByRole("button", { name: "Resume generation" }).click();
  await preparation.getByRole("button", { name: "Generate scripts" }).click();
  await expect(preparation.getByText("2 of 2 processed")).toBeVisible({
    timeout: 30_000,
  });
  for (const [person, path, correct, other] of [
    [
      "Avery Path",
      "founder",
      "How did you win your first customers?",
      "How do your reps qualify accounts?",
    ],
    [
      "Jordan Path",
      "sales-leader",
      "How do your reps qualify accounts?",
      "How did you win your first customers?",
    ],
  ]) {
    await page.getByRole("button", { name: `Open lead ${person}` }).click();
    const detail = page.getByRole("dialog", { name: "Lead packet" });
    await expect(detail.getByText("Script for this lead")).toBeVisible();
    await expect(detail.getByText(`Path · ${path}`)).toBeVisible();
    await expect(detail.getByText(correct)).toBeVisible();
    await expect(detail.getByText(other)).toHaveCount(0);
    await expect(detail.getByText("Busy", { exact: true })).toBeVisible();
    await detail.getByRole("button", { name: "Close", exact: true }).click();
  }
  await goTo(page, "Dialer");
  await page.getByLabel("Dial from all active lists").uncheck();
  await page.getByLabel("Dial list Script paths CSV").check();
  try {
    const started = page.waitForResponse((response) =>
      response.url().endsWith("/v1/calls/start"),
    );
    await startCalling(page).click();
    const receipt = await started;
    expect(receipt.ok(), await receipt.text()).toBeTruthy();
    const placed = await receipt.json();
    const lead = page.getByRole("region", { name: "Lead", exact: true });
    const script = page.getByTestId("prepared-script");
    await expect(
      script.getByText(/Path · (founder|sales-leader)/),
    ).toBeVisible();
    const founder =
      (await lead.getByTestId("lead-name").innerText()) === "Avery Path";
    await expect(lead).toContainText(founder ? "Brightfield" : "Redfield");
    await expect(lead).toContainText(founder ? "Founder" : "Sales Leader");
    await expect(lead).toContainText(
      founder ? "Builds warehouse software" : "Builds safety software",
    );
    await expect(lead).toContainText(
      founder ? "Announced a customer pilot" : "Opened an Austin office",
    );
    await expect(script).toContainText(
      founder ? "Path · founder" : "Path · sales-leader",
    );
    expect(placed.script_snapshot.script_path).toBe(
      founder ? "founder" : "sales-leader",
    );
    await expect(script).not.toContainText(
      founder ? "Builds warehouse software" : "Builds safety software",
    );
  } finally {
    await finishCall(page);
  }
});
