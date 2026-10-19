import { expect, test } from "@playwright/test";
import { prepareSimulationCall } from "./simulation";

test("fixed helper remains visible, private controls work and no raw content is archived", async ({
  page,
}) => {
  const exposureRequests: Record<string, unknown>[] = [];
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().endsWith("/coach/feedback")
    )
      exposureRequests.push(request.postDataJSON());
  });
  await page.setViewportSize({ width: 1280, height: 720 });
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
  await prepareSimulationCall(page, "Helper Fixture", "+14155550175");
  await page
    .getByRole("button", { name: "Get next lead", exact: true })
    .click();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  const opener = page.getByTestId("prepared-script").locator("blockquote");
  await expect(opener).toBeInViewport({ ratio: 1 });
  const openerBox = await opener.boundingBox();
  const hangupBox = await page
    .getByRole("button", { name: "Hang up", exact: true })
    .boundingBox();
  expect(openerBox!.y + openerBox!.height).toBeLessThan(hangupBox!.y);
  await page
    .getByLabel("Simulation transcript")
    .fill("Our approvals depend on a union committee.");
  await page.getByRole("button", { name: "Add prospect turn" }).click();
  const strip = page.getByTestId("live-coaching-strip");
  await expect(strip).toBeVisible();
  await page.mouse.wheel(0, 1200);
  await expect(strip).toBeInViewport();
  await expect(
    page.getByRole("button", { name: "Hang up", exact: true }),
  ).toBeInViewport();
  await page.getByRole("button", { name: "Mute tips", exact: true }).click();
  await expect(page.getByText("Automatic tips muted")).toBeVisible();
  await page.getByRole("button", { name: "Private help", exact: true }).click();
  await page
    .getByLabel("Private helper question")
    .fill("Which fact are we missing?");
  await page
    .getByRole("button", { name: "Ask privately", exact: true })
    .click();
  await expect(page.getByLabel("Private helper question")).toHaveValue("");
  const callView = /\/v1\/calls\/[^/]+$/;
  let reviewStatus = "model_suggestion";
  await page.route(callView, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (body.coach)
      body.coach = {
        ...body.coach,
        provider: "openrouter",
        meaning_review: { status: reviewStatus },
        alternatives: [
          "Which constraint matters most?",
          "What would make the next step worthwhile?",
        ],
      };
    await route.fulfill({ response, json: body });
  });
  for (const [status, label] of [
    ["model_suggestion", "Model suggestion"],
    ["model_reviewed", "Model-reviewed guidance"],
  ]) {
    reviewStatus = status;
    await page.keyboard.press("F1");
    await expect(strip.getByText(label, { exact: false })).toBeVisible();
    await expect(
      strip.getByText("Alternative: Which constraint matters most?", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Hang up", exact: true }),
    ).toBeInViewport();
  }
  await expect
    .poll(() => exposureRequests.some((r) => r.displayed === true))
    .toBe(true);
  await page
    .getByRole("button", { name: "I did not use this guidance", exact: true })
    .click();
  await expect(
    page.getByText("Guidance feedback saved", { exact: true }),
  ).toBeVisible();
  expect(exposureRequests.some((r) => r.used_by_rep === false)).toBe(true);
  expect(exposureRequests.every((r) => r.conversion === undefined)).toBe(true);
  await page.unrouteAll({ behavior: "wait" });
  await page.getByRole("button", { name: "Hang up", exact: true }).click();
  await page.getByLabel("Disposition").selectOption("no_answer");
  await page.getByRole("button", { name: "Save outcome", exact: true }).click();
  await page
    .getByRole("button", { name: "Pause dialing", exact: true })
    .click();
});

test("settings display the authority cost register, including changed rates", async ({
  page,
}) => {
  await page.route("**/v1/admin/operating-policy", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.costs.voice.api_per_minute_usd = 0.0037;
    body.costs.dnc.prepaid_minimum_usd = 123;
    body.rates_checked_at = "2026-11-02";
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
  await page.getByRole("button", { name: "Policy & DNC", exact: true }).click();
  await page.getByText("PAYG cost outline", { exact: true }).click();
  await expect(
    page.getByText("$0.0037/min API", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("$123 / 60,000 queries", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("Reference prices checked 2026-11-02", { exact: false }),
  ).toBeVisible();
});
