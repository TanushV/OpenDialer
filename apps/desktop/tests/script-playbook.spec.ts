import { openScriptSection } from "./navigation";
import { expect, test } from "./fixtures";
import { isolatedSimulationWorkspace } from "./simulation";

const script = `# Goal
Earn a 20-minute conversation.
# Opener
Hi {first_name}, {rep} from {seller}.
> Pause for permission, then listen.
# Problem and outcome
## Capacity
{{ one short reason based on research }}
## Alternative
We help with the work around each deal.
# Close
Worth a short conversation?
`;

test("a Markdown playbook shows non-spoken cues and measured rewrite usage", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await isolatedSimulationWorkspace(page, "playbook-rewrite-usage");
  await page.getByRole("button", { name: "Script", exact: true }).click();
  const editor = page.getByLabel("Script Markdown");
  await expect(editor).toBeVisible();
  const original = await editor.inputValue();
  let saved = false;
  try {
    await editor.fill(script);
    await expect(
      page.getByText("Rep directions", { exact: true }).first(),
    ).toBeVisible();
    await expect(
      page.getByText("Pause for permission, then listen.").first(),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Save script", exact: true })
      .click();
    await expect(
      page
        .getByRole("region", { name: "Your script", exact: true })
        .getByRole("status")
        .filter({ hasText: /Script saved/ }),
    ).toBeVisible();
    saved = true;
    await openScriptSection(page, "AI usage and script rewrites");
    const usage = page.getByRole("region", { name: "Model token usage" });
    await expect(usage).toBeVisible();
    await expect(
      usage.getByRole("button", {
        name: "Rewrite remaining leads",
        exact: true,
      }),
    ).toBeVisible();
    await usage
      .getByRole("button", { name: "Measure a 3-lead sample", exact: true })
      .click();
    await expect(page.getByText(/Sample queued/)).toBeVisible();
    await usage
      .getByRole("button", { name: "Refresh usage", exact: true })
      .click();
    await expect(
      usage.getByText("Input tokens", { exact: true }),
    ).toBeVisible();
    await expect(usage.getByText(/Historical preparation usage/)).toBeVisible();
  } finally {
    await editor.fill(original);
    if (saved) {
      await page
        .getByRole("button", { name: "Save script", exact: true })
        .click();
      await expect(
        page
          .getByRole("region", { name: "Your script", exact: true })
          .getByRole("status")
          .filter({ hasText: /Script saved/ }),
      ).toBeVisible();
    }
  }
});
