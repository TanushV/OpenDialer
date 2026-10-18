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
