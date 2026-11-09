import { expect, test } from "./fixtures";
import { isolatedSimulationWorkspace } from "./simulation";

test("the script editor adds the trigger list once and it saves", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await isolatedSimulationWorkspace(page, "trigger-list");
  await page.getByRole("button", { name: "Script", exact: true }).click();
  const editor = page.getByLabel("Script Markdown");
  await expect(editor).toBeVisible();
  const add = page.getByRole("button", { name: "Add trigger list" });
  await add.click();
  await expect(editor).toHaveValue(/# Triggers\n/);
  await expect(editor).toHaveValue(
    /- new_funding: New funding \(180 days, B\)/,
  );
  await expect(add).toBeHidden();
  await page.getByRole("button", { name: "Save script", exact: true }).click();
  await expect(
    page
      .getByRole("region", { name: "Your script", exact: true })
      .getByRole("status")
      .filter({ hasText: /Script saved/ }),
  ).toBeVisible();
});
