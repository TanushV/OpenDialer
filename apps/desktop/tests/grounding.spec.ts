import { expect, test } from "./fixtures";
import {
  answer,
  saveOutcome,
  hangUp,
  pauseCalling,
  prepareSimulationCall,
  startCalling,
} from "./simulation";

test("a failed evidence review is visible without overwriting the rep note", async ({
  page,
}) => {
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.grounding_status = "review_required";
    body.human_notes = "My own call notes remain the record.";
    body.call_memory = {
      rundown_points: [
        { text: "Procurement involves several teams.", status: "confirmed" },
      ],
      memory_omitted_items: 2,
    };
    body.interpretation_coverage = {
      total_segments: 10,
      interpreted_segments: 7,
      pending_segments: 1,
      unavailable_segments: 2,
    };
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
  await expect(page.getByRole("status", { name: "Call status" })).toBeVisible();
  await prepareSimulationCall(page, "Grounding Fixture", "+14155550174");
  await startCalling(page).click();
  await answer(page);
  await pauseCalling(page).click();
  await hangUp(page).click();
  await saveOutcome(page, "not_interested");
  await expect(startCalling(page)).toBeVisible();
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "What it has learned" }),
  ).toContainText("so far");
  await page
    .getByRole("button", { name: /^Review call / })
    .first()
    .click();
  await expect(
    page.getByRole("status", { name: "Evidence review" }),
  ).toContainText("AI interpretation needs review");
  await expect(page.getByLabel("Your notes", { exact: true })).toHaveValue(
    "My own call notes remain the record.",
  );
  await expect(
    page.getByRole("region", { name: "Call timeline", exact: true }),
  ).toContainText("Call prepared");
  const memory = page.getByRole("region", {
    name: "Retained call context",
    exact: true,
  });
  await expect(memory).toContainText("Procurement involves several teams.");
  await expect(memory).toContainText("7 of 10");
  await expect(memory).toContainText("2 could not be interpreted");
  await expect(memory).toContainText("2 older context items");

  // Losing an unprocessed chunk is different from ordinary expiry after analysis.
  await page.unroute("**/v1/calls/*/analysis");
  await page.route("**/v1/calls/*/analysis", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.call.analysis_incomplete = true;
    body.call.unprocessed_speech_events = 2;
    body.raw_evidence_status = "unavailable_after_restart";
    body.human_notes = "My own call notes remain the record.";
    await route.fulfill({ response, json: body });
  });
  await page.getByRole("button", { name: "All conversations" }).click();
  await page
    .getByRole("button", { name: /^Review call / })
    .first()
    .click();
  await expect(
    page.getByRole("status", { name: "Incomplete call analysis" }),
  ).toContainText("2 speech events could not be processed");
  await expect(
    page.getByRole("status", { name: "Incomplete call analysis" }),
  ).toContainText("Review your notes and follow-up");
  await expect(page.getByLabel("Your notes", { exact: true })).toHaveValue(
    "My own call notes remain the record.",
  );
});
