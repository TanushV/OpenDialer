import { expect, test } from "./fixtures";
import { prepareSimulationCall, chooseOutcome } from "./simulation";

test("leaving review prevents an in-flight refresh from reopening the old call", async ({
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
  await prepareSimulationCall(
    page,
    "Review navigation fixture",
    "+14155550179",
  );
  const reserved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/queue/next") && r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  const selected = await (await reserved).json();
  const reviewCall = page.getByRole("button", {
    name: `Review call ${selected.lead.name}`,
    exact: true,
  });
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  // The former implicit default outcome, now an explicit rep choice.
  await chooseOutcome(page, "right_party");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await reviewCall.click();
  await page
    .getByLabel("Actual call purpose", { exact: true })
    .selectOption("follow_up");
  await page
    .getByLabel("Reason for purpose correction", { exact: true })
    .fill("Rep corrected the conversation purpose.");
  let arrived!: () => void, release!: () => void;
  const entered = new Promise<void>((resolve) => {
    arrived = resolve;
  });
  const proceed = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    arrived();
    await proceed;
    await route.fulfill({ response });
  });
  await page
    .getByRole("button", { name: "Update call purpose", exact: true })
    .click();
  await entered;
  await page
    .getByRole("button", { name: "All conversations", exact: true })
    .click();
  const response = page.waitForResponse((r) => r.url().endsWith("/analysis"));
  release();
  await response;
  await expect(reviewCall).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Call purpose", exact: true }),
  ).toBeHidden();
});

test("campaign scoring anchors persist and a rep can correct the call purpose", async ({
  page,
}) => {
  let failRefresh = false;
  await page.route("**/v1/calls/*/analysis", async (route) => {
    if (failRefresh) {
      failRefresh = false;
      await route.fulfill({
        status: 503,
        json: { detail: "Review refresh unavailable" },
      });
      return;
    }
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
  await prepareSimulationCall(page, "Rubric Fixture", "+14155550176");
  const reserved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/v1/queue/next") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  const selected = await (await reserved).json();
  const reviewCall = page.getByRole("button", {
    name: `Review call ${selected.lead.name}`,
    exact: true,
  });
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await chooseOutcome(page, "right_party");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await reviewCall.click();
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
  let failSave = true;
  await page.route("**/v1/calls/*/rubric", async (route) => {
    if (failSave) {
      failSave = false;
      failRefresh = true;
      await route.fulfill({
        status: 503,
        json: { detail: "Purpose save unavailable" },
      });
    } else await route.continue();
  });
  await page
    .getByRole("button", { name: "Update call purpose", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Update call purpose", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Update call purpose", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Call purpose", exact: true }),
  ).toContainText("Follow up");
  await page
    .getByRole("button", { name: "All conversations", exact: true })
    .click();
  await reviewCall.click();
  await expect(
    page.getByLabel("Actual call purpose", { exact: true }),
  ).toHaveValue("follow_up");
  const review = page.getByRole("region", {
    name: "Human score review",
    exact: true,
  });
  await review.getByLabel("Review action", { exact: true }).selectOption("N/A");
  await review
    .getByLabel("Review dimension", { exact: true })
    .selectOption("problem_discovery");
  await review
    .getByLabel("Review reason", { exact: true })
    .fill("No new discovery opportunity was offered during this follow-up.");
  let loseReviewResponse = true;
  const reviewRequests: unknown[] = [];
  await page.route("**/v1/scorecards/*/review", async (route) => {
    reviewRequests.push(route.request().postDataJSON());
    if (loseReviewResponse) {
      loseReviewResponse = false;
      await route.fetch();
      await route.abort("connectionreset");
    } else await route.continue();
  });
  await review
    .getByRole("button", { name: "Save review", exact: true })
    .click();
  await expect(
    review.getByRole("button", { name: "Save review", exact: true }),
  ).toBeEnabled();
  await review
    .getByRole("button", { name: "Save review", exact: true })
    .click();
  expect(reviewRequests).toHaveLength(2);
  expect(reviewRequests[0]).toEqual(reviewRequests[1]);
  await expect(review).toContainText("Human rating");
  await expect(review).toContainText(
    "No new discovery opportunity was offered during this follow-up.",
  );
  await page
    .getByRole("button", { name: "All conversations", exact: true })
    .click();
  await reviewCall.click();
  await expect(review).toContainText(
    "No new discovery opportunity was offered during this follow-up.",
  );
  await expect(review).toContainText("AI assessment remains available above");
});
