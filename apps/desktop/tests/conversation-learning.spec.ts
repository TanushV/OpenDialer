import { expect, test } from "@playwright/test";
import { chooseOutcome } from "./simulation";

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

test("the rep can retract an interpreted response without editing the transcript", async ({
  page,
}) => {
  let retracted = false;
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.observations = [
      {
        id: "fixture-observation",
        version: retracted ? 2 : 1,
        concept: "Procurement approval",
        approach: "Clarify approval steps",
        status: retracted ? "retracted" : "active",
        review_status: "model_reviewed",
        observed_at: new Date().toISOString(),
        evidence: ["expired-source"],
      },
    ];
    await route.fulfill({ response, json: body });
  });
  await page.route(
    "**/observations/fixture-observation/review",
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        expected_version: 1,
        decision: "retract",
        reason: "The approach was not used in this call.",
      });
      retracted = true;
      await route.fulfill({ json: { status: "retracted" } });
    },
  );
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
  await page.getByRole("button", { name: "Go ready", exact: true }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "no_answer");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: /^Review call / })
    .first()
    .click();
  const section = page.getByRole("region", {
    name: "Conversation observations",
    exact: true,
  });
  await expect(section).toContainText("Clarify approval steps");
  await section
    .getByLabel("Observation review reason")
    .fill("The approach was not used in this call.");
  await section.getByRole("button", { name: "Retract observation" }).click();
  await expect(section).toContainText("Retracted");
  expect(retracted).toBe(true);
});
