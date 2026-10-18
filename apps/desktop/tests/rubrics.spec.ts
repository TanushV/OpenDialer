import { expect, test } from "@playwright/test";

test("campaign scoring anchors persist and a rep can correct the call purpose", async ({
  page,
}) => {
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.trackers = [
      {
        tracker_id: "confirmed-callback",
        name: "Explicit callback",
        kind: "semantic",
        version: 2,
        evidence: ["expired-source"],
        raw_evidence_status: "expired",
      },
    ];
    await route.fulfill({ response, json: body });
  });
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
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  const editor = page.getByRole("region", {
    name: "Call scoring rubrics",
    exact: true,
  });
  await editor
    .getByLabel("Rubric to edit", { exact: true })
    .selectOption("discovery");
  await editor
    .getByLabel("Clearly demonstrated anchor", { exact: true })
    .first()
    .fill("Confirmed the buyer's workflow and constraints.");
  await editor
    .getByRole("button", { name: "Save scoring rubrics", exact: true })
    .click();
  await expect(editor).toContainText("Rubrics saved");
  await page.reload();
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Operations", exact: true }).click();
  await editor
    .getByLabel("Rubric to edit", { exact: true })
    .selectOption("discovery");
  await expect(
    editor.getByLabel("Clearly demonstrated anchor", { exact: true }).first(),
  ).toHaveValue("Confirmed the buyer's workflow and constraints.");
  await page.getByRole("button", { name: "Dialer", exact: true }).click();
  await page.getByRole("button", { name: "Go ready", exact: true }).click();
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await page.getByLabel("Disposition").selectOption("right_party");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await page
    .getByRole("button", { name: /^Review call / })
    .first()
    .click();
  await expect(
    page.getByRole("region", { name: "Conversation signals", exact: true }),
  ).toContainText("Explicit callback");
  await expect(
    page.getByRole("region", { name: "Conversation signals", exact: true }),
  ).toContainText("Original speech expired");
  await page
    .getByLabel("Actual call purpose", { exact: true })
    .selectOption("follow_up");
  await page
    .getByLabel("Reason for purpose correction", { exact: true })
    .fill("This conversation continued an earlier evaluation.");
  await page
    .getByRole("button", { name: "Update call purpose", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Call purpose", exact: true }),
  ).toContainText("Follow up");
  await page
    .getByRole("button", { name: "All conversations", exact: true })
    .click();
  await page
    .getByRole("button", { name: /^Review call / })
    .first()
    .click();
  await expect(
    page.getByLabel("Actual call purpose", { exact: true }),
  ).toHaveValue("follow_up");
});
