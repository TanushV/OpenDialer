import { expect, test } from "./fixtures";
import { chooseOutcome, prepareSimulationCall } from "./simulation";
import { uncovered } from "./layout";

/** R0.12: at 1024x720 nothing may sit on top of the rep's next action. */
test.use({ viewport: { width: 1024, height: 720 } });

test("the dock never covers Save outcome and a toast never covers Next call", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  await expect(page.getByText("Opening your workspace…")).toBeHidden();
  const sample = page.getByRole("button", {
    name: "Try a simulated workspace",
  });
  if (await sample.isVisible()) await sample.click();
  await prepareSimulationCall(page, "Layout hotfix", "+14155550781");
  await page
    .getByRole("button", { name: "Next call Space", exact: true })
    .click();
  const hangup = page.getByRole("button", { name: "Hang up", exact: true });
  await expect(hangup).toBeVisible();
  await hangup.click();
  const save = page.getByRole("button", { name: "Save outcome", exact: true });
  await expect(save).toBeVisible();
  // The import notice is still showing: it must not cover the wrap-up either.
  await expect(page.locator(".toast")).toBeVisible();
  await uncovered(save);
  await page.screenshot({ path: testInfo.outputPath("wrap-up-1024x720.png") });
  await chooseOutcome(page, "no_answer");
  await save.click();
  await expect(save).toBeHidden();
  const next = page.getByRole("button", {
    name: "Next call Space",
    exact: true,
  });
  await expect(next).toBeVisible();
  await uncovered(next);
  await page.screenshot({
    path: testInfo.outputPath("next-call-1024x720.png"),
  });
});
