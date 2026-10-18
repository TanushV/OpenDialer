import { expect, test } from "@playwright/test";

test("conversation learning is honest about sparse evidence and saves reviewed aliases", async ({
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
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  const panel = page.getByRole("region", {
    name: "Conversation learning",
    exact: true,
  });
  await expect(panel).toContainText("No supported conversation patterns yet");
  await expect(panel).toContainText(
    "Correlations do not prove that an approach caused a conversion",
  );
  await panel.getByText("Review concept names", { exact: true }).click();
  await panel.getByLabel("Observed wording").fill("Purchasing signoff");
  await panel.getByLabel("Shared concept name").fill("Procurement approval");
  await panel
    .getByLabel("Normalization review reason")
    .fill("Same business process reviewed by the seller");
  await panel
    .getByRole("button", { name: "Save reviewed normalization" })
    .click();
  await expect(panel).toContainText(
    "Purchasing signoff → Procurement approval",
  );
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(panel).toContainText(
    "Purchasing signoff → Procurement approval",
  );
});
