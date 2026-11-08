import { expect, test } from "./fixtures";
import { chooseOutcome, prepareSimulationCall } from "./simulation";
import type { Locator } from "@playwright/test";

/** R0.12: at 1024x720 nothing may sit on top of the rep's next action. */
test.use({ viewport: { width: 1024, height: 720 } });

async function uncovered(target: Locator) {
  // Checked where the app leaves it, without test scrolling: top, middle and
  // bottom rows across the button, so a partial overlap still fails.
  const box = await target.boundingBox();
  expect(box).not.toBeNull();
  const points = [3, box!.height / 2, box!.height - 3].flatMap((dy) =>
    [0.1, 0.3, 0.5, 0.7, 0.9].map((fx) => ({
      x: box!.x + box!.width * fx,
      y: box!.y + dy,
    })),
  );
  const cover = await target.evaluate((element, points) => {
    for (const point of points) {
      const hit = document.elementFromPoint(point.x, point.y);
      if (hit && (element === hit || element.contains(hit))) continue;
      const owner = hit?.closest("[class]");
      return owner
        ? `${owner.tagName.toLowerCase()}.${owner.className}`
        : "nothing";
    }
    return "";
  }, points);
  expect(cover, `covers ${await target.textContent()}`).toBe("");
}

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
