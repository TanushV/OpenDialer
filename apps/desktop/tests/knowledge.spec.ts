import { expect, test } from "@playwright/test";

test("reviewed business knowledge and current version survive publication and reload", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await page
    .getByRole("textbox", {
      name: "Product and pricing knowledge",
      exact: true,
    })
    .fill(
      "Workflow training with three guided onboarding sessions. Pricing is confirmed by the rep.",
    );
  await page
    .getByRole("textbox", { name: "Approved factual claims", exact: true })
    .fill(
      "We provide workflow training.\nThree guided onboarding sessions are included.",
    );
  await page
    .getByRole("textbox", { name: "Limits and prohibited claims", exact: true })
    .fill("Do not promise a revenue increase.");
  await page
    .getByRole("textbox", { name: "Opener", exact: true })
    .fill(
      "Hello from Example Seller. Is this a good time for a quick introduction?",
    );
  await page
    .getByRole("textbox", { name: "Publication reason", exact: true })
    .fill("Reviewed product context for calling");
  await page
    .getByRole("button", { name: "Publish reviewed version", exact: false })
    .click();
  await expect(
    page.getByText(
      "New playbook version published. Active call scripts stay stable.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Intelligence", exact: true }).click();
  await expect(
    page.getByRole("textbox", {
      name: "Product and pricing knowledge",
      exact: true,
    }),
  ).toHaveValue(
    "Workflow training with three guided onboarding sessions. Pricing is confirmed by the rep.",
  );
  await expect(
    page.getByRole("textbox", { name: "Approved factual claims", exact: true }),
  ).toHaveValue(
    "We provide workflow training.\nThree guided onboarding sessions are included.",
  );
  await expect(
    page.getByRole("textbox", {
      name: "Limits and prohibited claims",
      exact: true,
    }),
  ).toHaveValue("Do not promise a revenue increase.");
  await expect(
    page.getByRole("textbox", { name: "Opener", exact: true }),
  ).toHaveValue(
    "Hello from Example Seller. Is this a good time for a quick introduction?",
  );
});
