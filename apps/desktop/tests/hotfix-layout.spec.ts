import { expect, test } from "./fixtures";
import {
  answer,
  finishCall,
  hangUp,
  prepareSimulationCall,
} from "./simulation";
import { settled, uncovered } from "./layout";

/** R0.12, kept for the new Dialer: at 1024x720 nothing may sit on top of the rep's next action. */
test.use({ viewport: { width: 1024, height: 720 } });

test("nothing covers the outcome keys or Dial now, even with a notice showing", async ({
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
    .getByRole("button", { name: "Start calling Space", exact: true })
    .click();
  await answer(page);
  await hangUp(page).click();
  const save = page.getByRole("button", {
    name: "3 Not interested",
    exact: true,
  });
  await expect(save).toBeVisible();
  // The import notice is still showing: it must not cover the outcome keys.
  await expect(page.locator(".toast")).toBeVisible();
  await settled(save);
  await uncovered(save);
  await page.screenshot({ path: testInfo.outputPath("wrap-up-1024x720.png") });
  await save.click();
  const next = page.getByRole("button", {
    name: "Dial now Space",
    exact: true,
  });
  await expect(next).toBeVisible();
  await settled(next);
  await uncovered(next);
  await page.screenshot({
    path: testInfo.outputPath("next-call-1024x720.png"),
  });
  await finishCall(page);
});
